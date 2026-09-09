"""Monte Carlo（M0：最小 seeded 路径生成器；完整引擎属 M3，Spec §8 / §12-7）。"""

from __future__ import annotations

import numpy as np


def generate_paths(
    *,
    seed: int,
    n_paths: int,
    days: int,
    s0: float,
    sigma: float,
    drift: float = 0.0,
    dt: float = 1.0 / 252.0,
    antithetic: bool = False,
) -> np.ndarray:
    """seeded GBM 路径生成（固定 seed 结果逐位一致，Spec §12-7）。

    返回 shape (n_paths, days + 1)，首列为 s0。
    antithetic=True 时前半与后半路径互为镜像（对数收益取反）。
    """
    rng = np.random.default_rng(seed)
    if antithetic:
        n = n_paths // 2
        z = rng.normal(size=(n, days))
        z = np.concatenate([z, -z], axis=0)
        if n_paths % 2:
            z = np.concatenate([z, rng.normal(size=(1, days))], axis=0)
    else:
        z = rng.normal(size=(n_paths, days))
    log_ret = (drift - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * z
    path = s0 * np.exp(
        np.concatenate([np.zeros((z.shape[0], 1)), np.cumsum(log_ret, axis=1)], axis=1)
    )
    return path
