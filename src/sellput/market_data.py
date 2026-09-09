"""Raw 市场数据契约与 Provider（Spec §4.0 / §4.1）。

Raw 字段（数据层契约）：timestamp、underlying price、bid/ask/last、strike、expiration、
option type、volume、open interest（+ 元数据 symbol/multiplier/settlement/style）。
IV 与 Greeks 不在此层 —— 由 pricing/vol 引擎计算（Derived，可重算）。

M0 实现：SyntheticProvider（seeded GBM 标的 + BS 定价的合成期权链）。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
from scipy.special import erf

from .config import BacktestConfig, DataConfig

if TYPE_CHECKING:  # 仅类型解析；运行时由 build_provider 懒加载（纯合成路径不碰 pandas 数据层）
    from .data import PriceSeries
from .instruments import (
    NyseCalendar,
    OptionId,
    OptionRight,
    TradingCalendar,
    defaults_for_symbol,
    monthly_expiries,
    underlier_kind,
)


class Session(Enum):
    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class UnderlierQuote:
    symbol: str
    price: float
    prev_close: float


@dataclass(frozen=True, slots=True)
class OptionQuote:
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """不可变市场快照（Spec §4.1）。

    铁律：信号只读 CloseSnapshot(t−1)；成交只用 OpenSnapshot(t)（Spec D9）。
    """

    date: date
    session: Session
    underlier: UnderlierQuote
    option_quotes: Mapping[OptionId, OptionQuote]
    rate: float
    dividend_yield: float

    def mid(self, oid: OptionId) -> float:
        return self.option_quotes[oid].mid


class MarketDataProvider(ABC):
    """Raw 数据接入抽象（Spec D4：可替换 Provider）。

    extra_strikes：需要保证有报价的 (expiry, strike) 集合（已挂牌/持仓合约，
    模拟真实期权链"挂牌后持续存在"的特性）；实现可忽略。
    """

    @abstractmethod
    def sessions(self) -> list[date]: ...

    @abstractmethod
    def close_snapshot(
        self, day: date, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot: ...

    @abstractmethod
    def open_snapshot(
        self, day: date, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot: ...

    @abstractmethod
    def expiries_available(self, day: date) -> list[date]: ...


class SyntheticChainEngine:
    """合成 Put 链报价器（BS 定价）。

    SyntheticProvider（M0）与 HybridProvider（M1-A）共用：
    链的形状参数固定，标的价格 / 波动率 / 股息率由调用方按日传入。
    全部随机数按 (seed, day, session) 派生 —— 跨日期独立、整体确定。
    """

    def __init__(
        self,
        *,
        skew: float,
        spread_bps: float,
        rate: float,
        strike_span: float,
        strikes_per_side: int,
        kind: str,
        seed: int,
    ) -> None:
        self.skew = skew
        self.spread_bps = spread_bps
        self.rate = rate
        self.strike_span = strike_span
        self.strikes_per_side = strikes_per_side
        self.kind = kind
        self.seed = seed

    def strike_step(self) -> float:
        return 5.0 if self.kind == "index" else 0.5

    def strike_grid(self, S: float) -> set[float]:
        """以 S 为锚的 ±span 网格（四舍五入到 strike 步长）。"""
        step = self.strike_step()
        n = self.strikes_per_side
        out: set[float] = set()
        for i in range(-n, n + 1):
            raw = S * (1.0 + i * self.strike_span / n)
            K = round(raw / step) * step
            if K > 0:
                out.add(K)
        return out

    def chain(
        self,
        *,
        day: date,
        session: Session,
        anchors: list[float],
        S: float,
        iv_atm: float,
        q: float,
        calendar: TradingCalendar,
    ) -> dict[OptionId, OptionQuote]:
        """合成链：anchors 为网格锚定价（开/收盘价），S 为本快照标的价。"""
        expiries = [
            e
            for e in monthly_expiries(day, day + timedelta(days=120))
            if 7 <= calendar.dte(day, e) <= 90
        ]
        strikes_by_expiry: dict[date, set[float]] = {}
        for e in expiries:
            ss: set[float] = set()
            for a in anchors:
                if a is not None and a > 0:
                    ss |= self.strike_grid(a)
            strikes_by_expiry[e] = ss

        rng = np.random.default_rng(
            [self.seed, 3, day.toordinal(), 0 if session is Session.OPEN else 1]
        )
        quotes: dict[OptionId, OptionQuote] = {}
        for e, ss in strikes_by_expiry.items():
            T = (e - day).days / 365.0
            if T <= 0:
                continue  # 到期日当天无需期权报价（到期处理只用标的价格）
            ks = np.array(sorted(ss))
            if len(ks) == 0:
                continue
            ivs = iv_atm + self.skew * (ks / S - 1.0)
            disc_q = math.exp(-q * T)
            disc_r = math.exp(-self.rate * T)
            sqT = math.sqrt(T)
            d1 = (np.log(S / ks) + (self.rate - q + 0.5 * ivs**2) * T) / (ivs * sqT)
            d2 = d1 - ivs * sqT
            for right in (OptionRight.PUT,):  # M0/M1 仅需 Put 报价；CALL 策略（M4）时扩展
                # erf 实现的解析正态 CDF
                n_m_d2 = 0.5 * (1.0 + erf(-d2 / math.sqrt(2.0)))
                n_m_d1 = 0.5 * (1.0 + erf(-d1 / math.sqrt(2.0)))
                mid = ks * disc_r * n_m_d2 - S * disc_q * n_m_d1
                half = mid * self.spread_bps / 1e4
                vols = rng.integers(10, 500, size=len(ks))
                ois = rng.integers(50, 5000, size=len(ks))
                for j, (K, m) in enumerate(zip(ks, mid, strict=True)):
                    oid: OptionId = (e, float(K), right)
                    quotes[oid] = OptionQuote(
                        bid=max(float(m) - half[j], 0.01),
                        ask=float(m) + half[j],
                        last=float(m),
                        volume=int(vols[j]),
                        open_interest=int(ois[j]),
                    )
        return quotes

    def extra_quotes(
        self,
        *,
        day: date,
        S: float,
        iv_atm: float,
        q: float,
        extra_strikes: set[tuple[date, float]],
        base: MarketSnapshot,
    ) -> dict[OptionId, OptionQuote]:
        """额外行权价的 Put 报价（确定性标量 BS，无随机数）。"""
        out: dict[OptionId, OptionQuote] = {}
        for e, k in sorted(extra_strikes):
            oid: OptionId = (e, k, OptionRight.PUT)
            if oid in base.option_quotes:
                continue
            T = (e - day).days / 365.0
            if T <= 0:
                continue  # 到期日当天无需期权报价（到期处理只用标的价格）
            iv = iv_atm + self.skew * (k / S - 1.0)
            sqT = math.sqrt(T)
            d1 = (math.log(S / k) + (self.rate - q + 0.5 * iv**2) * T) / (iv * sqT)
            d2 = d1 - iv * sqT
            # erf 实现的解析正态 CDF（快、精度 ~1e-15）
            n_m_d2 = 0.5 * (1.0 + math.erf(-d2 / math.sqrt(2.0)))
            n_m_d1 = 0.5 * (1.0 + math.erf(-d1 / math.sqrt(2.0)))
            mid = k * math.exp(-self.rate * T) * n_m_d2 - S * math.exp(-q * T) * n_m_d1
            half = mid * self.spread_bps / 1e4
            out[oid] = OptionQuote(
                bid=max(mid - half, 0.01), ask=mid + half, last=mid, volume=0, open_interest=0
            )
        return out


class SyntheticProvider(MarketDataProvider):
    """合成数据（开发与金标准测试；Spec D4 起步路径）。

    - 标的：seeded GBM 收盘路径（dt=1/252），开盘价 = 前收 × 隔夜噪声
    - 期权链：月度第三周五到期（DTE 7–90 可见），strike 网格 ±span（指数步长 5 / 其他 0.5），
      IV = iv_atm + skew×(K/S − 1)，mid = BS 价格，bid/ask 按 spread_bps 摊开
    - 全部随机数按 (seed, date, session) 派生：跨日期独立、整体确定（Spec §12-7）
    """

    def __init__(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        seed: int,
        s0: float,
        sigma: float,
        drift: float,
        iv_atm: float,
        skew: float = 0.0,
        spread_bps: float = 5.0,
        q: float = 0.0,
        rate: float = 0.04,
        strike_span: float = 0.15,
        strikes_per_side: int = 8,
        calendar: TradingCalendar | None = None,
        buffer_days: int = 40,
    ) -> None:
        self.symbol = symbol.upper()
        self.start, self.end = start, end
        self.seed = seed
        self.s0 = s0
        self.sigma = sigma
        self.drift = drift
        self.iv_atm = iv_atm
        self.skew = skew
        self.spread_bps = spread_bps
        self.q = q
        self.rate = rate
        self.strike_span = strike_span
        self.strikes_per_side = strikes_per_side
        self.calendar = calendar or NyseCalendar()
        self.kind = underlier_kind(self.symbol)
        self._style, _settlement = defaults_for_symbol(self.symbol)
        self._cache: dict[tuple[date, Session], MarketSnapshot] = {}
        self._engine = SyntheticChainEngine(
            skew=skew,
            spread_bps=spread_bps,
            rate=rate,
            strike_span=strike_span,
            strikes_per_side=strikes_per_side,
            kind=self.kind,
            seed=seed,
        )

        start_eff = start - timedelta(days=2 * buffer_days)
        self._all_sessions = self.calendar.sessions(start_eff, end)
        if not self._all_sessions:
            raise ValueError("no sessions in range")
        self._close: dict[date, float] = {}
        self._open: dict[date, float] = {}
        self._build_paths()

    # ---- 路径生成 ----

    def _build_paths(self) -> None:
        dt = 1.0 / 252.0
        z_close = np.random.default_rng([self.seed, 1]).normal(size=len(self._all_sessions))
        z_open = np.random.default_rng([self.seed, 2]).normal(size=len(self._all_sessions))
        s = self.s0
        for i, (d, zc, zo) in enumerate(zip(self._all_sessions, z_close, z_open, strict=True)):
            prev = self.s0 if i == 0 else s
            s = s * math.exp(
                (self.drift - 0.5 * self.sigma**2) * dt + self.sigma * math.sqrt(dt) * zc
            )
            self._close[d] = s
            self._open[d] = prev * math.exp(self.sigma * math.sqrt(dt) * zo)

    # ---- Provider 接口 ----

    def sessions(self) -> list[date]:
        return [d for d in self._all_sessions if self.start <= d <= self.end]

    def close_snapshot(
        self, day: date, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot:
        return self._snapshot(day, Session.CLOSE, extra_strikes)

    def open_snapshot(
        self, day: date, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot:
        return self._snapshot(day, Session.OPEN, extra_strikes)

    def expiries_available(self, day: date) -> list[date]:
        return sorted({oid[0] for oid in self.close_snapshot(day).option_quotes})

    def _snapshot(
        self, day: date, session: Session, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot:
        if extra_strikes:
            # 在缓存的基础链上增量合并额外行权价（持仓/下单合约，保证持续可报价）
            base = self._snapshot(day, session, None)
            extras = self._extra_quotes(day, session, extra_strikes, base)
            if not extras:
                return base
            merged = dict(base.option_quotes)
            merged.update(extras)
            return MarketSnapshot(
                date=base.date,
                session=base.session,
                underlier=base.underlier,
                option_quotes=merged,
                rate=base.rate,
                dividend_yield=base.dividend_yield,
            )
        key = (day, session)
        if key in self._cache:
            return self._cache[key]
        S = self._open[day] if session is Session.OPEN else self._close[day]
        prev_day = self.calendar.prev_session(day)
        prev_close = self._close.get(prev_day, S)
        snap = MarketSnapshot(
            date=day,
            session=session,
            underlier=UnderlierQuote(self.symbol, S, prev_close),
            option_quotes=self._chain(day, session),
            rate=self.rate,
            dividend_yield=self.q,
        )
        # 只保留近期快照，控制长回测内存（sim 只需 t−1 / t 两个交易日窗口）
        cutoff = day - timedelta(days=10)
        for k in list(self._cache):
            if k[0] < cutoff:
                del self._cache[k]
        self._cache[key] = snap
        return snap

    def _extra_quotes(
        self,
        day: date,
        session: Session,
        extra_strikes: set[tuple[date, float]],
        base: MarketSnapshot,
    ) -> dict[OptionId, OptionQuote]:
        S = self._open[day] if session is Session.OPEN else self._close[day]
        return self._engine.extra_quotes(
            day=day, S=S, iv_atm=self.iv_atm, q=self.q,
            extra_strikes=extra_strikes, base=base,
        )

    # ---- 合成链（委托给 SyntheticChainEngine） ----

    def _chain(self, day: date, session: Session) -> dict[OptionId, OptionQuote]:
        S = self._open[day] if session is Session.OPEN else self._close[day]
        # 基础网格锚：当日开/收盘价。持仓/下单合约经 extra_strikes 保证持续可报价
        # （真实期权链已挂牌合约持续存在，Spec §4.0 连续性）。
        anchors = [
            v for v in (self._open.get(day), self._close.get(day)) if v is not None and v > 0
        ]
        return self._engine.chain(
            day=day, session=session, anchors=anchors, S=S,
            iv_atm=self.iv_atm, q=self.q, calendar=self.calendar,
        )


def synthetic_provider_from_config(cfg: DataConfig, rate: float) -> SyntheticProvider:
    s = cfg.synthetic
    return SyntheticProvider(
        symbol=cfg.symbol,
        start=cfg.start,
        end=cfg.end,
        seed=s.seed,
        s0=s.s0,
        sigma=s.sigma,
        drift=s.drift,
        iv_atm=s.iv_atm,
        skew=s.skew,
        spread_bps=s.spread_bps,
        q=s.q,
        rate=rate,
        strike_span=s.strike_span,
        strikes_per_side=s.strikes_per_side,
    )


class HybridProvider(MarketDataProvider):
    """M1-A：真实标的日线 + 合成期权链（Spec §13，与用户确认的设计）。

    - 标的价格：PriceSeries（yfinance / 手动 CSV / 本地缓存，未复权口径）
    - 波动率：滚动窗口（默认 20 交易日）已实现波动率，年化；
      open 快照用 t−1 的值 —— 开盘时尚未知今日收盘，防前视（Spec D9）
    - 股息率：过去 12 个月实际分红 ÷ 前收（"截至前收"口径，防前视）
    - 期权链：SyntheticChainEngine（iv_atm = 滚动已实现波动率）
    """

    def __init__(
        self,
        *,
        series: PriceSeries,
        start: date,
        end: date,
        skew: float = 0.0,
        spread_bps: float = 5.0,
        rate: float = 0.04,
        strike_span: float = 0.15,
        strikes_per_side: int = 8,
        vol_window: int = 20,
        seed: int = 42,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self.symbol = series.symbol.upper()
        self.kind = underlier_kind(self.symbol)
        self.calendar = calendar or NyseCalendar()
        self.rate = rate
        self.vol_window = vol_window
        self.start, self.end = start, end
        self._engine = SyntheticChainEngine(
            skew=skew,
            spread_bps=spread_bps,
            rate=rate,
            strike_span=strike_span,
            strikes_per_side=strikes_per_side,
            kind=self.kind,
            seed=seed,
        )
        self._dates: list[date] = list(series.dates)
        self._pos = {d: i for i, d in enumerate(self._dates)}
        self._close = {d: float(c) for d, c in zip(self._dates, series.close, strict=True)}
        self._open = {d: float(o) for d, o in zip(self._dates, series.open, strict=True)}
        self._dividends = dict(series.dividends)
        self._sigmas = self._rolling_sigma(series)
        self._q_cache: dict[date, float] = {}
        if self._dates:
            self.q = float(np.mean([self.dividend_yield_at(d) for d in self._dates]))
        else:
            self.q = 0.0
        self._cache: dict[tuple[date, Session], MarketSnapshot] = {}

    # ---- 波动率 / 股息率（防前视口径） ----

    def _rolling_sigma(self, series: PriceSeries) -> dict[date, float]:
        closes = series.close
        rets = np.diff(np.log(closes))
        out: dict[date, float] = {}
        w = self.vol_window
        for i, d in enumerate(series.dates):
            seg = rets[max(0, i - w) : i]
            if len(seg) >= 2:
                out[d] = float(np.std(seg, ddof=1) * math.sqrt(252.0))
            else:
                out[d] = 0.2  # 数据缓冲不足（仅序列起点）时回退
        return out

    def sigma_at(self, day: date) -> float:
        """day 收盘后已知的滚动已实现波动率（年化）。"""
        return self._sigmas[day]

    def dividend_yield_at(self, day: date) -> float:
        """截至前收的连续股息率近似：过去 12 个月分红 ÷ 前收。

        直接遍历分红记录（量级 ~几百条，无性能问题），
        兼容除息日不在价格序列中的情况。
        """
        cached = self._q_cache.get(day)
        if cached is not None:
            return cached
        prev_close = self._prev_close(day)
        if prev_close <= 0:
            return 0.0
        window_start = day - timedelta(days=365)
        divs = sum(v for d, v in self._dividends.items() if window_start < d <= day)
        q = divs / prev_close
        self._q_cache[day] = q
        return q

    def _prev_day(self, day: date) -> date:
        pos = self._pos.get(day)
        if pos is None or pos == 0:
            return day
        return self._dates[pos - 1]

    def _prev_close(self, day: date) -> float:
        return self._close[self._prev_day(day)]

    # ---- Provider 接口 ----

    def sessions(self) -> list[date]:
        return [d for d in self._dates if self.start <= d <= self.end]

    def close_snapshot(
        self, day: date, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot:
        return self._snapshot(day, Session.CLOSE, extra_strikes)

    def open_snapshot(
        self, day: date, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot:
        return self._snapshot(day, Session.OPEN, extra_strikes)

    def expiries_available(self, day: date) -> list[date]:
        return sorted({oid[0] for oid in self.close_snapshot(day).option_quotes})

    def _snapshot(
        self, day: date, session: Session, extra_strikes: set[tuple[date, float]] | None = None
    ) -> MarketSnapshot:
        if extra_strikes:
            # 在缓存的基础链上增量合并额外行权价（持仓/下单合约，保证持续可报价）
            base = self._snapshot(day, session, None)
            S = self._open[day] if session is Session.OPEN else self._close[day]
            ref_day = self._prev_day(day) if session is Session.OPEN else day
            extras = self._engine.extra_quotes(
                day=day, S=S, iv_atm=self._sigmas[ref_day],
                q=self.dividend_yield_at(ref_day), extra_strikes=extra_strikes, base=base,
            )
            if not extras:
                return base
            merged = dict(base.option_quotes)
            merged.update(extras)
            return MarketSnapshot(
                date=base.date,
                session=base.session,
                underlier=base.underlier,
                option_quotes=merged,
                rate=base.rate,
                dividend_yield=base.dividend_yield,
            )
        key = (day, session)
        if key in self._cache:
            return self._cache[key]
        if day not in self._pos:
            raise ValueError(
                f"{self.symbol} 数据中没有 {day}（请检查数据缓冲/缓存覆盖范围，"
                f"数据起于 {self._dates[0]}）"
            )
        prev_day = self._prev_day(day)
        prev_close = self._close.get(prev_day, self._close[day])
        if session is Session.OPEN:
            # 防前视：开盘快照只用开盘时已知的信息（今日开盘价、前收、
            # 截至前收的波动率与股息率）—— 绝不使用今日收盘价。
            S = self._open[day]
            ref_day = prev_day
            anchors = [v for v in (self._open.get(day), prev_close) if v is not None and v > 0]
        else:
            S = self._close[day]
            ref_day = day
            anchors = [
                v for v in (self._open.get(day), self._close.get(day))
                if v is not None and v > 0
            ]
        q = self.dividend_yield_at(ref_day)
        snap = MarketSnapshot(
            date=day,
            session=session,
            underlier=UnderlierQuote(self.symbol, S, prev_close),
            option_quotes=self._engine.chain(
                day=day, session=session, anchors=anchors, S=S,
                iv_atm=self._sigmas[ref_day], q=q, calendar=self.calendar,
            ),
            rate=self.rate,
            dividend_yield=q,
        )
        # 只保留近期快照，控制长回测内存（sim 只需 t−1 / t 两个交易日窗口）
        cutoff = day - timedelta(days=10)
        for k in list(self._cache):
            if k[0] < cutoff:
                del self._cache[k]
        self._cache[key] = snap
        return snap

    def prices_frame(self):
        """导出实际使用的价格序列（审计/复现用）。"""
        import pandas as pd  # 局部导入：合成路径不依赖 pandas 展示层

        return pd.DataFrame(
            {
                "Date": self._dates,
                "Open": [self._open[d] for d in self._dates],
                "Close": [self._close[d] for d in self._dates],
                "sigma": [self._sigmas[d] for d in self._dates],
                "dividend_yield": [self.dividend_yield_at(d) for d in self._dates],
            }
        )


def build_provider(cfg: BacktestConfig) -> MarketDataProvider:
    """按配置构建数据 Provider（synthetic / hybrid）。"""
    d = cfg.data
    if d.provider == "synthetic":
        return synthetic_provider_from_config(d, cfg.market.rate)
    if d.provider == "hybrid":
        from .data import load_price_series  # 懒加载：纯合成路径不触碰网络层

        series = load_price_series(
            symbol=d.symbol,
            start=d.start,
            end=d.end,
            cache_dir=d.hybrid.cache_dir,
            csv_path=d.hybrid.csv_path,
            buffer_days=d.hybrid.buffer_days,
            allow_network=not d.hybrid.offline,
        )
        return HybridProvider(
            series=series,
            start=d.start,
            end=d.end,
            skew=d.synthetic.skew,
            spread_bps=d.synthetic.spread_bps,
            rate=cfg.market.rate,
            strike_span=d.synthetic.strike_span,
            strikes_per_side=d.synthetic.strikes_per_side,
            vol_window=d.hybrid.vol_window,
            seed=d.synthetic.seed,
        )
    raise ValueError(f"unknown provider: {d.provider}")
