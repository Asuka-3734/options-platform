"""CRR 对拍（Spec §12-1）：收敛、美式性质、与 BS 的关系。"""

from __future__ import annotations

import pytest

from sellput.instruments import OptionRight, OptionStyle
from sellput.pricing import BlackScholesEngine, CRRBinomialEngine

BS = BlackScholesEngine()


def test_european_crr_converges_to_bs():
    S, K, T, r, q, sigma = 100.0, 100.0, 0.25, 0.04, 0.0, 0.20
    bs = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=OptionRight.CALL).price
    errs = []
    for steps in (50, 200, 800):
        crr = CRRBinomialEngine(steps).price(
            S=S, K=K, T=T, r=r, q=q, sigma=sigma,
            right=OptionRight.CALL, style=OptionStyle.EUROPEAN,
        ).price
        errs.append(abs(crr - bs))
    assert errs[0] > errs[1] > errs[2]  # 单调收敛
    assert errs[2] < 2e-2


def test_american_put_dominates_european():
    engine = CRRBinomialEngine(200)
    base = dict(S=100.0, K=100.0, T=0.5, r=0.05, q=0.0, sigma=0.20, right=OptionRight.PUT)
    am = engine.price(style=OptionStyle.AMERICAN, **base).price
    eu = engine.price(style=OptionStyle.EUROPEAN, **base).price
    assert am >= eu


def test_american_put_equals_european_when_rates_zero():
    # r=q=0 时提前行权永不最优 → 美式 == 欧式（精确相等，Spec §12-1）
    engine = CRRBinomialEngine(200)
    base = dict(S=100.0, K=95.0, T=0.25, r=0.0, q=0.0, sigma=0.20, right=OptionRight.PUT)
    am = engine.price(style=OptionStyle.AMERICAN, **base).price
    eu = engine.price(style=OptionStyle.EUROPEAN, **base).price
    assert am == pytest.approx(eu, abs=1e-12)


def test_american_call_equals_european_call_no_dividends():
    # q=0 时美式看涨提前行权永不最优 → 美式 == 欧式（Spec §12-1）
    engine = CRRBinomialEngine(200)
    base = dict(S=100.0, K=100.0, T=0.5, r=0.05, q=0.0, sigma=0.25, right=OptionRight.CALL)
    am = engine.price(style=OptionStyle.AMERICAN, **base).price
    eu = engine.price(style=OptionStyle.EUROPEAN, **base).price
    assert am == pytest.approx(eu, abs=1e-12)


def test_crr_greeks_sanity():
    res = CRRBinomialEngine(200).price(
        S=100, K=95, T=0.25, r=0.04, q=0.0, sigma=0.20,
        right=OptionRight.PUT, style=OptionStyle.AMERICAN,
    )
    assert -1.0 <= res.greeks.delta <= 0.0
    assert res.greeks.gamma >= 0.0
    assert res.greeks.vega >= 0.0


def test_crr_delta_gamma_vs_bs():
    """回归：CRR delta/gamma 取自树第 1/2 层，须与 BS 解析值收敛（曾错捕获顶层→恒 0）。"""
    kw = dict(S=100.0, K=95.0, T=0.25, r=0.04, q=0.0, sigma=0.20, right=OptionRight.PUT)
    bs = BS.price(**kw)
    crr = CRRBinomialEngine(200).price(**kw, style=OptionStyle.EUROPEAN)
    assert crr.greeks.delta == pytest.approx(bs.greeks.delta, abs=1e-2)
    assert crr.greeks.gamma == pytest.approx(bs.greeks.gamma, rel=0.08)
    # 非零（若捕获层级错误会得到 0.0）
    assert abs(crr.greeks.delta) > 0.1
    assert crr.greeks.gamma > 0.01


def test_crr_greeks_finite_difference():
    """CRR delta/gamma/vega/theta 与中心差分一致（Spec §12-2）。

    注意 h 必须跨越多个格距（u−1)·S≈0.7）：CRR 价格对 S 呈锯齿状，
    小步长 FD 会落在单个锯齿内（二阶差≈0），是 lattice 伪影而非实现错误。
    """
    engine = CRRBinomialEngine(200)
    base = dict(S=100.0, K=95.0, T=0.25, r=0.04, q=0.0, sigma=0.20,
                right=OptionRight.PUT, style=OptionStyle.AMERICAN)
    p0 = engine.price(**base)
    h_s, h_t = 2.0, 1.0 / 365.0
    up = engine.price(**{**base, "S": base["S"] + h_s}).price
    dn = engine.price(**{**base, "S": base["S"] - h_s}).price
    later = engine.price(**{**base, "T": base["T"] + h_t}).price
    # 树 delta/gamma 有 O(1/n) 离散化振荡；容差放宽到足以捕获
    # "层级错捕→恒 0"回归（≈100% 误差），又不误报正常离散化。
    assert p0.greeks.delta == pytest.approx((up - dn) / (2 * h_s), rel=3e-2)
    assert p0.greeks.gamma == pytest.approx((up - 2 * p0.price + dn) / h_s**2, rel=0.15)
    assert p0.greeks.theta == pytest.approx((later - p0.price) / h_t, rel=1e-2)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        CRRBinomialEngine(200).price(
            S=100, K=95, T=0.0, r=0.04, q=0.0, sigma=0.2, right=OptionRight.PUT
        )
    with pytest.raises(ValueError):
        CRRBinomialEngine(1)
