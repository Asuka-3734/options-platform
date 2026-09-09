"""期权定价与风险指标（Spec D6 / 附录 C）。

M0 实现：
- PricingEngine ABC
- BlackScholesEngine：欧式解析价格 + 解析 Greeks；对美式标的可用作近似，
  此时 PriceResult.model 标注 "black_scholes_approx"（非严格美式定价，Spec D6）
- CRRBinomialEngine：标准 CRR（Spec D6：第一版标准实现，不加入高级 early-exercise 优化）；
  支持美式/欧式风格；Greeks 用有限差分
- implied_vol：BS 口径反解（市场惯例；对美式标的同样用 BS 反解，注明近似）
- forward

约定：T 以年计（日历日 / 365）；theta/vega/rho 为年化口径。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from .instruments import OptionRight, OptionStyle

_IV_LOWER, _IV_UPPER = 1e-4, 5.0
_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    """标准正态 CDF（erf 实现，快且精度 ~1e-15）。"""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


@dataclass(frozen=True, slots=True)
class PriceResult:
    price: float
    greeks: Greeks
    model: str  # "black_scholes" | "black_scholes_approx" | "crr_binomial"


class PricingEngine(ABC):
    model: str

    @abstractmethod
    def price(
        self,
        *,
        S: float,
        K: float,
        T: float,
        r: float,
        q: float,
        sigma: float,
        right: OptionRight,
        style: OptionStyle,
    ) -> PriceResult: ...


def _check_inputs(S: float, K: float, T: float, sigma: float) -> None:
    if S <= 0 or K <= 0:
        raise ValueError("S and K must be positive")
    if T <= 0:
        raise ValueError(
            f"T must be positive (got {T}); expired options must be settled before pricing"
        )
    if sigma <= 0:
        raise ValueError("sigma must be positive")


class BlackScholesEngine(PricingEngine):
    """欧式 Black-Scholes（解析价格与解析 Greeks，附录 C.1）。"""

    model = "black_scholes"

    def price(
        self,
        *,
        S: float,
        K: float,
        T: float,
        r: float,
        q: float,
        sigma: float,
        right: OptionRight,
        style: OptionStyle = OptionStyle.EUROPEAN,
    ) -> PriceResult:
        _check_inputs(S, K, T, sigma)
        is_call = right is OptionRight.CALL
        sqT = math.sqrt(T)
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sqT)
        d2 = d1 - sigma * sqT
        N1, N2 = _ncdf(d1), _ncdf(d2)
        N1m, N2m = _ncdf(-d1), _ncdf(-d2)
        pdf1 = _npdf(d1)
        disc_q = math.exp(-q * T)
        disc_r = math.exp(-r * T)

        if is_call:
            price = S * disc_q * N1 - K * disc_r * N2
            delta = disc_q * N1
            theta = (
                -S * disc_q * pdf1 * sigma / (2 * sqT)
                - r * K * disc_r * N2
                + q * S * disc_q * N1
            )
            rho = K * T * disc_r * N2
        else:
            price = K * disc_r * N2m - S * disc_q * N1m
            delta = disc_q * (N1 - 1)
            theta = (
                -S * disc_q * pdf1 * sigma / (2 * sqT)
                + r * K * disc_r * N2m
                - q * S * disc_q * N1m
            )
            rho = -K * T * disc_r * N2m

        gamma = disc_q * pdf1 / (S * sigma * sqT)
        vega = S * disc_q * pdf1 * sqT
        model = self.model if style is OptionStyle.EUROPEAN else "black_scholes_approx"
        return PriceResult(price=price, greeks=Greeks(delta, gamma, theta, vega, rho), model=model)


class CRRBinomialEngine(PricingEngine):
    """标准 CRR 二叉树（附录 C.2）。

    - 美式：回代时与内在价值取 max；欧式：不回代取 max（用于收敛测试）
    - 实现：批量向量化树 —— 基准与 vega/theta/rho 有限差分扰动共用同一次
      回代循环；delta/gamma 由基准树的第一/二层节点值解析提取
      （标准 CRR，无高级 early-exercise 优化，Spec D6）
    - price_strikes：一次批量计算多个行权价的期权价格（选价排序用）
    """

    model = "crr_binomial"

    def __init__(self, steps: int = 200) -> None:
        if steps < 2:
            raise ValueError("steps must be >= 2")
        self.steps = steps

    def price(
        self,
        *,
        S: float,
        K: float,
        T: float,
        r: float,
        q: float,
        sigma: float,
        right: OptionRight,
        style: OptionStyle = OptionStyle.AMERICAN,
    ) -> PriceResult:
        _check_inputs(S, K, T, sigma)
        h_sig, h_r, d_t = 1e-4, 1e-4, 1.0 / 365.0
        out, l1, l2, s_u, s_d, s_uu, s_ud, s_dd = self._tree_batch(
            params=[
                dict(S=S, K=K, T=T, r=r, q=q, sigma=sigma),
                dict(S=S, K=K, T=T, r=r, q=q, sigma=sigma + h_sig),
                dict(S=S, K=K, T=T + d_t, r=r, q=q, sigma=sigma),
                dict(S=S, K=K, T=T, r=r + h_r, q=q, sigma=sigma),
            ],
            right=right,
            style=style,
            capture_levels=True,
        )
        price0 = float(out[0])
        vega = (out[1] - price0) / h_sig
        theta = (out[2] - price0) / d_t
        rho = (out[3] - price0) / h_r
        delta = (l1[0] - l1[1]) / (s_u - s_d)
        gamma = ((l2[0] - l2[1]) / (s_uu - s_ud) - (l2[1] - l2[2]) / (s_ud - s_dd)) / (
            0.5 * (s_uu - s_dd)
        )
        return PriceResult(
            price=price0, greeks=Greeks(delta, gamma, theta, vega, rho), model=self.model
        )

    def price_strikes(
        self,
        *,
        S: float,
        Ks: np.ndarray,
        T: float,
        r: float,
        q: float,
        sigma: float,
        right: OptionRight,
        style: OptionStyle = OptionStyle.AMERICAN,
    ) -> np.ndarray:
        """批量定价多个行权价（同 S/T/σ）：返回与 Ks 同形的价格数组。"""
        _check_inputs(S, float(Ks[0]), T, sigma)
        out, *_ = self._tree_batch(
            params=[dict(S=S, K=float(k), T=T, r=r, q=q, sigma=sigma) for k in Ks],
            right=right,
            style=style,
            capture_levels=False,
        )
        return out

    def _tree_batch(
        self,
        *,
        params: list[dict],
        right: OptionRight,
        style: OptionStyle,
        capture_levels: bool,
    ) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None, float, float, float, float, float]:
        n = self.steps
        Ss = np.array([p["S"] for p in params])
        Ks = np.array([p["K"] for p in params])
        Ts = np.array([p["T"] for p in params])
        rs = np.array([p["r"] for p in params])
        qs = np.array([p["q"] for p in params])
        sigs = np.array([p["sigma"] for p in params])
        dt = Ts / n
        u = np.exp(sigs * np.sqrt(dt))
        d = 1.0 / u
        p = (np.exp((rs - qs) * dt) - d) / (u - d)
        disc = np.exp(-rs * dt)
        if np.any(~((p > 0.0) & (p < 1.0))):
            raise ValueError("CRR probability out of bounds (increase steps)")

        j = np.arange(n + 1)
        s = Ss[:, None] * (u[:, None] ** (n - j)[None, :] * d[:, None] ** j[None, :])
        is_call = right is OptionRight.CALL
        v = np.maximum(s - Ks[:, None], 0.0) if is_call else np.maximum(Ks[:, None] - s, 0.0)
        l1: np.ndarray | None = None
        l2: np.ndarray | None = None
        s1 = s2 = None
        # 免分配缓冲（out= 复用，避免每步数组分配与 GC 抖动）
        buf_a = np.empty_like(v)
        buf_b = np.empty_like(v)
        buf_s = np.empty_like(s)
        for step in range(n - 1, -1, -1):
            k = step + 1
            np.multiply(p[:, None], v[:, :k], out=buf_a[:, :k])
            np.multiply((1.0 - p)[:, None], v[:, 1 : k + 1], out=buf_b[:, :k])
            np.add(buf_a[:, :k], buf_b[:, :k], out=buf_a[:, :k])
            np.multiply(disc[:, None], buf_a[:, :k], out=buf_b[:, :k])
            np.divide(s[:, :k], u[:, None], out=buf_s[:, :k])  # 只取活跃宽度（level step+1）
            s, buf_s = buf_s, s
            if style is OptionStyle.AMERICAN:
                if is_call:
                    np.subtract(s[:, :k], Ks[:, None], out=buf_a[:, :k])
                else:
                    np.subtract(Ks[:, None], s[:, :k], out=buf_a[:, :k])
                np.maximum(buf_b[:, :k], buf_a[:, :k], out=buf_b[:, :k])
            v, buf_b = buf_b, v  # 交换：v 指向新值，buf_b 复用旧缓冲
            if capture_levels:
                # 回代至第 1/2 层时捕获（root 层之外最近的两层），
                # 用于解析提取 delta/gamma（Spec 附录 C.2）。
                if step == 1:
                    l1 = v[0].copy()
                    s1 = s[0].copy()
                elif step == 2:
                    l2 = v[0].copy()
                    s2 = s[0].copy()
        if capture_levels:
            assert l1 is not None and l2 is not None and s1 is not None and s2 is not None
            return (
                v[:, 0],
                l1,
                l2,
                float(s1[0]),
                float(s1[1]),
                float(s2[0]),
                float(s2[1]),
                float(s2[2]),
            )
        return v[:, 0], l1, l2, 0.0, 0.0, 0.0, 0.0, 0.0


def implied_vol(
    *,
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    right: OptionRight,
    tol: float = 1e-8,
    max_iter: int = 100,
) -> float:
    """BS 口径反解 IV（市场惯例；对美式标的同样用 BS 反解，注明近似）。

    牛顿法 + 二分兜底。返回年化 σ。
    """
    if S <= 0 or K <= 0 or T <= 0 or price <= 0:
        raise ValueError("S, K, T, price must be positive")
    # 无套利下界用"折现内在价值"（欧式期权价格可低于未折现内在价值，如深度实值 + 正利率）
    if right is OptionRight.PUT:
        lower = max(0.0, K * math.exp(-r * T) - S * math.exp(-q * T))
    else:
        lower = max(0.0, S * math.exp(-q * T) - K * math.exp(-r * T))
    # 深实值 + 极低波动率时，报价的 erf 尾部饱和可能比下界低 ~1e-14（浮点噪声，
    # M1-A 真实低波动行情会触发）；允许极小容差，真正违背无套利（差量级更大）仍拒绝。
    fuzz = 1e-9 * max(1.0, abs(lower))
    if price < lower - fuzz:
        raise ValueError("price below no-arbitrage lower bound")
    if price < lower:
        price = lower  # 视作贴在下界的报价（IV≈0）
    engine = BlackScholesEngine()
    sigma = 0.2
    for _ in range(max_iter):
        res = engine.price(S=S, K=K, T=T, r=r, q=q, sigma=sigma, right=right)
        diff = res.price - price
        if abs(diff) < tol * max(1.0, price):
            return sigma
        vega = res.greeks.vega
        sigma_new = sigma - diff / vega if vega > 1e-12 else sigma * (1.1 if diff < 0 else 0.9)
        if not (_IV_LOWER <= sigma_new <= _IV_UPPER):
            return _bisect_iv(price=price, S=S, K=K, T=T, r=r, q=q, right=right, engine=engine)
        sigma = sigma_new
    raise RuntimeError(f"IV did not converge (price={price})")


def _bisect_iv(*, price, S, K, T, r, q, right, engine: BlackScholesEngine) -> float:
    lo, hi = _IV_LOWER, _IV_UPPER
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p = engine.price(S=S, K=K, T=T, r=r, q=q, sigma=mid, right=right).price
        if p < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def forward(S: float, T: float, r: float, q: float) -> float:
    return S * math.exp((r - q) * T)
