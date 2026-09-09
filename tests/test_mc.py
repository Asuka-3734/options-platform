"""MC seed 可复现（Spec §12-7）。"""

from __future__ import annotations

import numpy as np

from sellput.mc import generate_paths


def test_same_seed_identical():
    a = generate_paths(seed=42, n_paths=100, days=252, s0=100.0, sigma=0.2)
    b = generate_paths(seed=42, n_paths=100, days=252, s0=100.0, sigma=0.2)
    assert np.array_equal(a, b)


def test_different_seed_differs():
    a = generate_paths(seed=42, n_paths=50, days=100, s0=100.0, sigma=0.2)
    b = generate_paths(seed=43, n_paths=50, days=100, s0=100.0, sigma=0.2)
    assert not np.array_equal(a, b)


def test_shape_and_start():
    p = generate_paths(seed=7, n_paths=10, days=30, s0=100.0, sigma=0.2)
    assert p.shape == (10, 31)
    assert np.all(p[:, 0] == 100.0)


def test_antithetic_mirror():
    n = 100
    sigma, dt = 0.2, 1.0 / 252.0
    p = generate_paths(seed=42, n_paths=n, days=50, s0=100.0, sigma=sigma, antithetic=True)
    half = n // 2
    for t in range(51):
        # 对数收益镜像：S_i(t) × S_{i+half}(t) = s0² · e^(−σ²·t·dt)（漂移项不镜像）
        expected = 100.0**2 * np.exp(-(sigma**2) * t * dt)
        assert np.allclose(p[:half, t] * p[half : 2 * half, t], expected)
