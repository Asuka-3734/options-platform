"""分红模型（Spec D17）。

架构要求：DividendModel 为可替换组件；不假设 ETF 与指数永远共用同一分红模型，
每个标的可绑定不同的 DividendModel 实例。

M0：仅 ContinuousYieldDividendModel（连续分红率 q）；
discrete dividend_schedule 接口为 M4 DiscreteDividendModel 预留。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date


class DividendModel(ABC):
    """分红模型抽象。"""

    name: str = "dividend_model"

    @abstractmethod
    def yield_rate(self, ts: date) -> float:
        """ts 时点的年化连续分红率 q。"""

    def dividend_schedule(self, ts: date, horizon: date) -> list[tuple[date, float]]:
        """未来 (ex_date, per_share) 分红列表 —— 为 DiscreteDividendModel 预留（M4）。

        M0 不支持，调用即抛 NotImplementedError（Spec D17）。
        """
        raise NotImplementedError("discrete dividend schedule 不在 M0 范围（Spec D17）")


class ContinuousYieldDividendModel(DividendModel):
    """连续分红率模型（M0 唯一实现）。"""

    name = "continuous_yield"

    def __init__(self, q: float) -> None:
        if q < 0:
            raise ValueError("q must be >= 0")
        self.q = q

    def yield_rate(self, ts: date) -> float:  # noqa: ARG002 常数模型与时间无关
        return self.q


class PerDayDividendModel(DividendModel):
    """按交易日查询股息率（Provider 派生值，如真实分红折算，M1-A）。

    get_rate(ts) 必须已满足防前视口径（实现方负责，如"截至前收"）。
    """

    name = "per_day"

    def __init__(self, get_rate) -> None:
        self._get_rate = get_rate

    def yield_rate(self, ts: date) -> float:
        return float(self._get_rate(ts))
