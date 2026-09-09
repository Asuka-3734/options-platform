"""防前视（Spec §12-3）：信号/执行快照分离 + 数据平移 + 策略对收盘数据的纯度。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from sellput.config import SellPutParams
from sellput.market_data import OptionQuote, Session, UnderlierQuote
from sellput.sim import SimulationEngine
from sellput.strategy import SellPutStrategy
from tests.conftest import (
    RecordingStrategy,
    StaticProvider,
    WeekdayCalendar,
    constant_world,
    make_config,
)

EXPIRY = date(2025, 2, 21)  # 01-06 决策时 dte = 32（±14 窗口内）
STRIKES = [90.0, 92.0, 94.0, 95.0, 96.0, 98.0, 100.0, 102.0, 104.0]


class OpenMutationProvider:
    """只篡改 Open(t) 的期权报价与标的价格（Close 原样透传）。"""

    def __init__(self, inner, quote_factor: float = 10.0, price_factor: float = 1.5) -> None:
        self.inner = inner
        self.quote_factor = quote_factor
        self.price_factor = price_factor

    def sessions(self):
        return self.inner.sessions()

    def close_snapshot(self, day, extra_strikes=None):
        return self.inner.close_snapshot(day, extra_strikes)

    def expiries_available(self, day):
        return self.inner.expiries_available(day)

    def open_snapshot(self, day, extra_strikes=None):
        snap = self.inner.open_snapshot(day, extra_strikes)
        quotes = {
            oid: OptionQuote(
                bid=q.bid * self.quote_factor,
                ask=q.ask * self.quote_factor,
                last=q.last * self.quote_factor,
                volume=q.volume,
                open_interest=q.open_interest,
            )
            for oid, q in snap.option_quotes.items()
        }
        u = UnderlierQuote(
            snap.underlier.symbol,
            snap.underlier.price * self.price_factor,
            snap.underlier.prev_close,
        )
        return replace(snap, option_quotes=quotes, underlier=u)


def test_signals_keyed_to_prev_close_execution_to_open(monkeypatch):
    """结构断言：信号全部基于 Close(t−1)；成交全部基于 Open(t)；篡改 Open 只影响成交。"""
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 1, 6), EXPIRY)
    base = constant_world(cal, sessions, expiry=EXPIRY, strikes=STRIKES, S=100.0)
    mutated = OpenMutationProvider(base)
    cfg = make_config(start=sessions[0], end=sessions[-1])
    cfg.strategy.params = SellPutParams(entry_frequency="when_free")

    rec_a = RecordingStrategy(SellPutStrategy(cfg.strategy.params))
    rec_b = RecordingStrategy(SellPutStrategy(cfg.strategy.params))
    monkeypatch.setattr("sellput.sim.build_strategy", lambda c: rec_a)
    result_a = SimulationEngine(cfg).run(base, cal)
    monkeypatch.setattr("sellput.sim.build_strategy", lambda c: rec_b)
    result_b = SimulationEngine(cfg).run(mutated, cal)

    # 1) 每个决策日的信号输入都是 t−1 的收盘快照
    assert [d for d, _ in rec_a.log] == [cal.prev_session(t) for t in sessions]
    assert [d for d, _ in rec_b.log] == [cal.prev_session(t) for t in sessions]
    # 2) 首日（状态相同、收盘数据相同）→ 信号完全一致
    assert rec_a.log[0][1] == rec_b.log[0][1]
    # 3) 篡改 Open 确实改变了成交价（执行层使用 Open 数据）
    prices_a = [o.fill.price for o in result_a.orders if o.fill is not None]
    prices_b = [o.fill.price for o in result_b.orders if o.fill is not None]
    assert prices_a and prices_b
    assert sum(prices_a) != sum(prices_b)
    # 4) 所有成交都发生在 Open 时点
    for o in [*result_a.orders, *result_b.orders]:
        if o.fill is not None:
            assert o.fill.session is Session.OPEN


def test_strategy_purity_on_prev_close(monkeypatch):
    """策略是 prev_close 的纯函数：相同输入 → 相同意图（确定性）。"""
    cal = WeekdayCalendar()
    today = date(2025, 1, 8)  # dte = 30
    cfg = make_config(start=today, end=EXPIRY)
    cfg.strategy.params = SellPutParams(entry_frequency="when_free")

    logs = []
    for _ in range(2):
        rec = RecordingStrategy(SellPutStrategy(cfg.strategy.params))
        logs.append(rec)
        prov = constant_world(
            cal, cal.sessions(today, EXPIRY), expiry=EXPIRY, strikes=STRIKES, S=100.0
        )
        monkeypatch.setattr("sellput.sim.build_strategy", lambda c, rec=rec: rec)
        SimulationEngine(cfg).run(prov, cal)
    assert [i for _, i in logs[0].log] == [i for _, i in logs[1].log]  # 同数据同结果


def test_data_shift_does_not_change_decisions(monkeypatch):
    cal = WeekdayCalendar()
    days = cal.sessions(date(2025, 1, 6), EXPIRY)
    d0 = cal.prev_session(days[0])
    prov_a = constant_world(cal, days, expiry=EXPIRY, strikes=STRIKES, S=100.0)

    # B：数据整体前移一个交易日（B 在 d 的数据 = A 在 next(d) 的数据）
    sessions_b = [d0, *days[:-2]]
    prov_b = StaticProvider(cal)
    prov_b.set_sessions(sessions_b)
    prev_b = cal.prev_session(sessions_b[0])
    for d in [prev_b, *sessions_b]:
        nd = cal.next_session(d)
        for sess in (Session.OPEN, Session.CLOSE):
            s = prov_a.get(nd, sess)
            prov_b.set(
                d, sess, s.underlier.price,
                {oid: (q.bid, q.ask) for oid, q in s.option_quotes.items()},
                prev_close=s.underlier.prev_close,
            )

    cfg = make_config(start=days[0], end=days[-1])
    cfg.strategy.params = SellPutParams(entry_frequency="when_free")
    rec_a = RecordingStrategy(SellPutStrategy(cfg.strategy.params))
    rec_b = RecordingStrategy(SellPutStrategy(cfg.strategy.params))
    monkeypatch.setattr("sellput.sim.build_strategy", lambda c: rec_a)
    SimulationEngine(cfg).run(prov_a, cal)
    monkeypatch.setattr("sellput.sim.build_strategy", lambda c: rec_b)
    SimulationEngine(cfg).run(prov_b, cal)

    intents_a = [intents for _, intents in rec_a.log]
    intents_b = [intents for _, intents in rec_b.log]
    # B 第 i 天决策 == A 第 i 天决策（数据整体平移 → 决策集整体平移）
    assert len(intents_b) == len(intents_a) - 1
    assert intents_b == intents_a[:-1]
