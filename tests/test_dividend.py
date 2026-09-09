"""分红模型（Spec D17）：连续 q 方向性 + 抽象接口 + discrete 预留。"""

from __future__ import annotations

from datetime import date

import pytest

from sellput.dividend import ContinuousYieldDividendModel
from sellput.instruments import OptionRight
from sellput.pricing import BlackScholesEngine, CRRBinomialEngine


def test_continuous_model_returns_q():
    m = ContinuousYieldDividendModel(0.015)
    assert m.yield_rate(date(2025, 1, 1)) == 0.015


def test_q_increases_put_decreases_call():
    bs = BlackScholesEngine()
    base = dict(S=100.0, K=100.0, T=0.25, r=0.04, sigma=0.20)
    put0 = bs.price(q=0.0, right=OptionRight.PUT, **base).price
    put1 = bs.price(q=0.02, right=OptionRight.PUT, **base).price
    call0 = bs.price(q=0.0, right=OptionRight.CALL, **base).price
    call1 = bs.price(q=0.02, right=OptionRight.CALL, **base).price
    assert put1 > put0
    assert call1 < call0


def test_q_direction_in_crr():
    crr = CRRBinomialEngine(200)
    base = dict(S=100.0, K=100.0, T=0.25, r=0.04, sigma=0.20)
    put0 = crr.price(q=0.0, right=OptionRight.PUT, **base).price
    put1 = crr.price(q=0.02, right=OptionRight.PUT, **base).price
    assert put1 > put0


def test_discrete_schedule_reserved():
    m = ContinuousYieldDividendModel(0.01)
    with pytest.raises(NotImplementedError):
        m.dividend_schedule(date(2025, 1, 1), date(2025, 6, 1))
