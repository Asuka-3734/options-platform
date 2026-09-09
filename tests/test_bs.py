"""BS 对拍与 Greeks sanity（Spec §12-1 / §12-2）。"""

from __future__ import annotations

import math

import pytest

from sellput.instruments import OptionRight, OptionStyle
from sellput.pricing import BlackScholesEngine

BS = BlackScholesEngine()


def test_hull_known_values():
    # Hull 教材算例：S=42, K=40, r=0.10, q=0, σ=0.20, T=0.5 → call 4.76 / put 0.81
    call = BS.price(S=42, K=40, T=0.5, r=0.10, q=0.0, sigma=0.20, right=OptionRight.CALL)
    put = BS.price(S=42, K=40, T=0.5, r=0.10, q=0.0, sigma=0.20, right=OptionRight.PUT)
    assert call.price == pytest.approx(4.7594, abs=1e-3)
    assert put.price == pytest.approx(0.8086, abs=1e-3)


@pytest.mark.parametrize(
    "S,K,T,r,q,sigma",
    [
        (100.0, 100.0, 0.25, 0.04, 0.0, 0.20),
        (100.0, 95.0, 0.25, 0.04, 0.0, 0.20),
        (120.0, 100.0, 1.0, 0.05, 0.02, 0.35),
        (80.0, 100.0, 2.0, 0.01, 0.03, 0.50),
        (100.0, 100.0, 30 / 365, 0.04, 0.0, 0.10),
    ],
)
def test_put_call_parity(S, K, T, r, q, sigma):
    call = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=OptionRight.CALL).price
    put = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=OptionRight.PUT).price
    assert call - put == pytest.approx(
        S * math.exp(-q * T) - K * math.exp(-r * T), abs=1e-10
    )


def test_greeks_finite_difference():
    S, K, T, r, q, sigma = 100.0, 100.0, 0.25, 0.04, 0.0, 0.20
    hS = 1e-4 * S
    for right in (OptionRight.CALL, OptionRight.PUT):
        res = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=right)
        up = BS.price(S=S + hS, K=K, T=T, r=r, q=q, sigma=sigma, right=right)
        dn = BS.price(S=S - hS, K=K, T=T, r=r, q=q, sigma=sigma, right=right)
        delta_fd = (up.price - dn.price) / (2 * hS)
        gamma_fd = (up.price - 2 * res.price + dn.price) / hS**2
        vega_fd = (
            BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma + 1e-4, right=right).price
            - res.price
        ) / 1e-4
        dt = 1e-4
        theta_fd = (
            BS.price(S=S, K=K, T=T + dt, r=r, q=q, sigma=sigma, right=right).price
            - res.price
        ) / dt  # ∂C/∂T = −theta
        assert res.greeks.delta == pytest.approx(delta_fd, rel=1e-4)
        assert res.greeks.gamma == pytest.approx(gamma_fd, rel=1e-3)
        assert res.greeks.vega == pytest.approx(vega_fd, rel=1e-3)
        assert res.greeks.theta == pytest.approx(-theta_fd, rel=1e-2)


def test_greeks_bounds_and_signs():
    S, K, T, r, q, sigma = 100.0, 100.0, 0.25, 0.04, 0.0, 0.20
    for right in (OptionRight.CALL, OptionRight.PUT):
        res = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=right)
        assert -1.0 <= res.greeks.delta <= 1.0
        assert res.greeks.gamma >= 0.0
        assert res.greeks.vega >= 0.0
    call_theta = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=OptionRight.CALL).greeks.theta
    assert call_theta < 0.0  # q=0 时多头看涨 theta 为负


def test_parity_greek_relation():
    S, K, T, r, q, sigma = 100.0, 95.0, 0.5, 0.04, 0.02, 0.25
    delta_c = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=OptionRight.CALL).greeks.delta
    delta_p = BS.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=OptionRight.PUT).greeks.delta
    assert delta_c - delta_p == pytest.approx(math.exp(-q * T), abs=1e-12)


def test_american_style_flagged_as_approximation():
    # Spec D6：BS 用于美式标的时必须标注近似（black_scholes_approx）
    res = BS.price(
        S=100, K=95, T=0.25, r=0.04, q=0.0, sigma=0.20,
        right=OptionRight.PUT, style=OptionStyle.AMERICAN,
    )
    assert res.model == "black_scholes_approx"
    eu = BS.price(S=100, K=95, T=0.25, r=0.04, q=0.0, sigma=0.20, right=OptionRight.PUT)
    assert eu.model == "black_scholes"
