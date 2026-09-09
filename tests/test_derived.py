"""Raw → Derived 可重算（Spec §12-8）。"""

from __future__ import annotations

from datetime import date

import pytest

from sellput.instruments import NyseCalendar, OptionRight
from sellput.market_data import SyntheticProvider
from sellput.pricing import BlackScholesEngine, implied_vol


def test_provider_iv_roundtrip_from_raw_mid():
    prov = SyntheticProvider(
        symbol="SPY",
        start=date(2025, 1, 2),
        end=date(2025, 3, 31),
        seed=42,
        s0=100.0,
        sigma=0.2,
        drift=0.0,
        iv_atm=0.25,
        spread_bps=5.0,
        q=0.0,
        rate=0.04,
    )
    snap = prov.close_snapshot(date(2025, 1, 15))
    oid = next(o for o in snap.option_quotes if o[2] is OptionRight.PUT)
    e, K, right = oid
    mid = snap.mid(oid)
    iv = implied_vol(
        price=mid,
        S=snap.underlier.price,
        K=K,
        T=(e - snap.date).days / 365.0,
        r=0.04,
        q=0.0,
        right=right,
    )
    # 链由 iv_atm=0.25 生成 → 从 Raw mid 反解应还原（Derived 可重算）
    assert iv == pytest.approx(0.25, abs=1e-6)


def test_greeks_recompute_deterministic():
    eng = BlackScholesEngine()
    r1 = eng.price(S=100, K=95, T=30 / 365, r=0.04, q=0.0, sigma=0.2, right=OptionRight.PUT)
    r2 = eng.price(S=100, K=95, T=30 / 365, r=0.04, q=0.0, sigma=0.2, right=OptionRight.PUT)
    assert r1 == r2


def test_nyse_calendar_used_by_provider():
    cal = NyseCalendar()
    prov = SyntheticProvider(
        symbol="SPY",
        start=date(2025, 1, 2),
        end=date(2025, 1, 31),
        seed=1,
        s0=100.0,
        sigma=0.2,
        drift=0.0,
        iv_atm=0.2,
        q=0.0,
        rate=0.04,
        calendar=cal,
    )
    sessions = prov.sessions()
    assert date(2025, 1, 20) not in sessions  # MLK 假日
    assert len(sessions) > 0
