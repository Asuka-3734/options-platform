"""期权合约与日历静态信息（M0 最小可用模型，Spec §4.1）。

- OptionSpec：合约规格（frozen dataclass）
- defaults_for_symbol：SPX/NDX → 欧式现金结算；其余（SPY/QQQ/个股）→ 美式实物交割
- TradingCalendar：交易日历抽象 + NYSE 实现（pandas_market_calendars，预计算 + 二分查找）
- third_friday / monthly_expiries：月度到期日（第三周五）
"""

from __future__ import annotations

import bisect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

import pandas_market_calendars as mcal


class OptionRight(Enum):
    CALL = "C"
    PUT = "P"


class OptionStyle(Enum):
    EUROPEAN = "european"
    AMERICAN = "american"


class Settlement(Enum):
    CASH = "cash"
    PHYSICAL = "physical"


@dataclass(frozen=True, slots=True)
class OptionSpec:
    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    style: OptionStyle
    multiplier: int = 100
    settlement: Settlement = Settlement.PHYSICAL

    @property
    def option_id(self) -> tuple[date, float, OptionRight]:
        return (self.expiry, self.strike, self.right)


OptionId = tuple[date, float, OptionRight]

# 宽基指数：欧式、现金结算（M0 默认规则；其余指数后续补充）
_INDEX_DEFAULTS: dict[str, tuple[OptionStyle, Settlement]] = {
    "SPX": (OptionStyle.EUROPEAN, Settlement.CASH),
    "NDX": (OptionStyle.EUROPEAN, Settlement.CASH),
}

_INDEX_SYMBOLS = frozenset(_INDEX_DEFAULTS)


def defaults_for_symbol(symbol: str) -> tuple[OptionStyle, Settlement]:
    """SPX/NDX → 欧式现金结算；其余（SPY/QQQ/个股）→ 美式实物交割。"""
    return _INDEX_DEFAULTS.get(symbol.upper(), (OptionStyle.AMERICAN, Settlement.PHYSICAL))


def underlier_kind(symbol: str) -> str:
    """'index'（宽基指数，现金结算）或 'equity'（ETF/个股）。"""
    return "index" if symbol.upper() in _INDEX_SYMBOLS else "equity"


class TradingCalendar(ABC):
    """交易日历抽象（M0 最小接口）。"""

    @abstractmethod
    def sessions(self, start: date, end: date) -> list[date]: ...

    @abstractmethod
    def is_session(self, day: date) -> bool: ...

    def next_session(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_session(d):
            d += timedelta(days=1)
        return d

    def prev_session(self, day: date) -> date:
        d = day - timedelta(days=1)
        while not self.is_session(d):
            d -= timedelta(days=1)
        return d

    def dte(self, today: date, expiry: date) -> int:
        """剩余交易日数：today 之后（不含）到 expiry（含）之间的交易日数；到期日当天为 0。"""
        if expiry <= today:
            return 0
        return len(self.sessions(today + timedelta(days=1), expiry))


class NyseCalendar(TradingCalendar):
    """NYSE 交易日历：一次预计算（1990–2060）+ 二分查找，避免逐日 schedule 调用。"""

    def __init__(
        self,
        name: str = "NYSE",
        span: tuple[date, date] = (date(1990, 1, 1), date(2060, 1, 1)),
    ) -> None:
        self._cal = mcal.get_calendar(name)
        sched = self._cal.schedule(start_date=span[0], end_date=span[1])
        self._sessions = sorted(d.date() for d in sched.index)

    def sessions(self, start: date, end: date) -> list[date]:
        lo = bisect.bisect_left(self._sessions, start)
        hi = bisect.bisect_right(self._sessions, end)
        return list(self._sessions[lo:hi])

    def is_session(self, day: date) -> bool:
        i = bisect.bisect_left(self._sessions, day)
        return i < len(self._sessions) and self._sessions[i] == day


def third_friday(year: int, month: int) -> date:
    """该月第三个周五（美股标准月度到期日）。"""
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7  # 周五 weekday == 4
    return first + timedelta(days=offset + 14)


def monthly_expiries(start: date, end: date) -> list[date]:
    """[start, end] 范围内所有月度第三个周五。"""
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        exp = third_friday(y, m)
        if start <= exp <= end:
            out.append(exp)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out
