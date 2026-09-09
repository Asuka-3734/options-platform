"""IV 反解往返（Spec §12-8：Raw → Derived 可重算的定价部分）。"""

from __future__ import annotations

import math

import pytest

from sellput.instruments import OptionRight
from sellput.pricing import BlackScholesEngine, implied_vol


def test_iv_roundtrip():
    engine = BlackScholesEngine()
    for sigma in (0.05, 0.2, 0.4, 0.8):
        for right in (OptionRight.CALL, OptionRight.PUT):
            price = engine.price(
                S=100, K=100, T=0.25, r=0.04, q=0.0, sigma=sigma, right=right
            ).price
            iv = implied_vol(price=price, S=100, K=100, T=0.25, r=0.04, q=0.0, right=right)
            assert iv == pytest.approx(sigma, abs=1e-8)


def test_iv_moneyness_grid():
    for K in (80.0, 95.0, 105.0, 120.0):
        price = BlackScholesEngine().price(
            S=100, K=K, T=30 / 365, r=0.04, q=0.0, sigma=0.25, right=OptionRight.PUT
        ).price
        iv = implied_vol(
            price=price, S=100, K=K, T=30 / 365, r=0.04, q=0.0, right=OptionRight.PUT
        )
        assert iv == pytest.approx(0.25, abs=1e-6)


def test_iv_rejects_below_no_arbitrage_bound():
    # 欧式 Put 下界 = K·e^(−rT) − S·e^(−qT) ≈ 8.905；价格 8.0 低于下界 → 拒绝
    with pytest.raises(ValueError):
        implied_vol(price=8.0, S=100, K=110, T=0.25, r=0.04, q=0.0, right=OptionRight.PUT)


def test_iv_tolerates_saturated_tail_fuzz():
    """回归（M1-A 真实低波动行情触发）：深实值 + 极低 IV 时报价的 erf 尾部
    饱和可能比无套利下界低 ~1e-14；视为贴在下界的报价（IV≈0），不得抛错。
    """
    lower = 110.0 * math.exp(-0.04 * 0.08) - 100.0  # K·e^(−rT) − S
    iv = implied_vol(
        price=lower - 1e-12, S=100, K=110, T=0.08, r=0.04, q=0.0, right=OptionRight.PUT
    )
    assert iv < 0.1  # 收敛到近零波动率（价格容差内无法区分 σ=0）
    roundtrip = BlackScholesEngine().price(
        S=100, K=110, T=0.08, r=0.04, q=0.0, sigma=iv, right=OptionRight.PUT
    ).price
    assert roundtrip == pytest.approx(lower, abs=1e-6)
    # 明显违背无套利（差几分钱）仍然拒绝
    with pytest.raises(ValueError):
        implied_vol(price=lower - 0.5, S=100, K=110, T=0.08, r=0.04, q=0.0, right=OptionRight.PUT)
