"""M1-C 步骤 7：MDD 约束筛选与"无可行解"测试（Spec v0.7 §15 F4 / AC-6）。

覆盖：只读 train 段筛选、阈值边界（含边界即满足）、按指标降序、**可行集为空时显式报"无可行解"
且不自动放宽阈值、不用"最接近"冒充可行解**、最接近组合的选取、参数校验与话术（§17.2 无预测原则）。
"""

from __future__ import annotations

from datetime import date

import pytest

from sellput.analysis import Metrics
from sellput.research import (
    INFEASIBLE_MESSAGE,
    SELECTION_DISCLAIMER,
    SegmentMetrics,
    SelectionView,
    SweepResult,
    select_by_constraint,
)


def make_metrics(
    *,
    cagr: float | None = 0.05,
    max_dd: float = -0.20,
    total_return: float = 0.50,
    win_rate: float | None = 0.9,
    n_trades: int = 50,
) -> Metrics:
    return Metrics(
        total_return=total_return,
        cagr=cagr,
        ann_vol=0.20,
        sharpe=0.30,
        sortino=0.40,
        calmar=None,
        max_dd=max_dd,
        dd_duration_days=200,
        n_trades=n_trades,
        win_rate=win_rate,
        profit_factor=1.5,
        avg_win=100.0,
        avg_loss=-50.0,
        trades_per_year=5.0,
        p5_trade_pnl=-300.0,
        worst_trade_pnl=-1_000.0,
        benchmark_price_return=0.30,
        benchmark_total_return=None,
        excess_vs_price=total_return - 0.30,
        excess_vs_total=None,
        capital_efficiency=10.0,
        start=date(2005, 1, 3),
        end=date(2018, 12, 31),
        n_sessions=3500,
    )


def make_result(combo: int, dte: int, delta: float, tp: float, metrics: Metrics) -> SweepResult:
    segments = SegmentMetrics(
        full=metrics, train=metrics, test=None, train_end=date(2018, 12, 31)
    )
    return SweepResult(
        combo=combo,
        params={
            "dte_target": dte,
            "delta_target": delta,
            "profit_target_pct": tp,
            "dte_exit": 3,
            "entry_frequency": "weekly",
            "max_open_positions": 1,
        },
        params_label=f"DTE {dte} / delta {delta:.2f} / 止盈 {tp:.0%}",
        segments=segments,
        run_dir=f"combo-{combo:03d}",
    )


def grid_fixture() -> list[SweepResult]:
    """4 个组合：MDD 分别 −25% / −35% / −85% / −95%，CAGR 各不相同。"""
    return [
        make_result(0, 30, 0.20, 0.50, make_metrics(cagr=0.04, max_dd=-0.25)),
        make_result(1, 30, 0.30, 0.50, make_metrics(cagr=0.08, max_dd=-0.35)),
        make_result(2, 45, 0.20, 0.50, make_metrics(cagr=0.03, max_dd=-0.85)),
        make_result(3, 45, 0.30, 0.50, make_metrics(cagr=0.06, max_dd=-0.95)),
    ]


# ---------------------------------------------------------------------------
# 1. 筛选与排序
# ---------------------------------------------------------------------------


def test_select_filters_on_train_mdd_and_sorts_by_cagr():
    view = select_by_constraint(grid_fixture(), max_dd=0.30, sort_by="cagr")

    assert isinstance(view, SelectionView)
    assert view.has_solution
    assert [r.combo for r in view.feasible] == [0]  # 只有 MDD −25% 满足 ≤30%
    assert view.total == 4
    assert view.n_filtered_out == 3
    assert view.constraint_text() == "train 最大回撤 ≥ −30.00%"
    assert view.no_solution_message() is None


def test_select_threshold_boundary_is_inclusive_and_never_relaxed():
    """恰好等于阈值算满足；比阈值差一点即被排除（不做任何"放宽"）。"""
    results = [
        make_result(0, 30, 0.20, 0.50, make_metrics(cagr=0.05, max_dd=-0.30)),  # 恰好
        make_result(1, 30, 0.20, 0.50, make_metrics(cagr=0.05, max_dd=-0.3000001)),
    ]
    view = select_by_constraint(results, max_dd=0.30)
    assert [r.combo for r in view.feasible] == [0]


def test_select_sorts_descending_and_puts_missing_values_last():
    results = [
        make_result(0, 30, 0.20, 0.50, make_metrics(cagr=0.05, max_dd=-0.20)),
        make_result(1, 30, 0.30, 0.50, make_metrics(cagr=0.11, max_dd=-0.20)),
        make_result(2, 45, 0.20, 0.50, make_metrics(cagr=None, max_dd=-0.20)),
    ]
    view = select_by_constraint(results, max_dd=0.50, sort_by="cagr")
    assert [r.combo for r in view.feasible] == [1, 0, 2]


def test_select_without_constraint_returns_all_sorted():
    view = select_by_constraint(grid_fixture(), max_dd=None, sort_by="cagr")
    assert len(view.feasible) == 4
    assert [r.combo for r in view.feasible] == [1, 3, 0, 2]
    assert view.constraint_text() == "无约束"


def test_select_can_sort_by_drawdown():
    view = select_by_constraint(grid_fixture(), max_dd=None, sort_by="max_dd")
    assert [r.combo for r in view.feasible] == [0, 1, 2, 3]  # 回撤从小到大（降序即最轻在前）


# ---------------------------------------------------------------------------
# 2. AC-6：无可行解必须显式输出
# ---------------------------------------------------------------------------


def test_select_reports_no_solution_explicitly():
    view = select_by_constraint(grid_fixture(), max_dd=0.20, sort_by="cagr")

    assert not view.has_solution
    assert view.feasible == ()
    message = view.no_solution_message()
    assert message is not None
    assert INFEASIBLE_MESSAGE in message
    assert "没有任何一个满足" in message
    assert "不会自动放宽阈值" in message
    assert "最接近约束 ≠ 满足约束" in message
    # 最接近的组合是 MDD 最轻的那个（−25%，combo 0），但明确不是可行解
    assert "DTE 30 / delta 0.20" in message
    assert "-25.00%" in message
    assert view.closest is not None and view.closest.combo == 0


def test_closest_is_the_least_bad_drawdown_not_the_best_return():
    """最接近约束 = MDD 最轻者，与收益排序无关（防止"顺手"把高收益组合当答案）。"""
    results = [
        make_result(0, 30, 0.20, 0.50, make_metrics(cagr=0.20, max_dd=-0.90)),  # 收益最高
        make_result(1, 30, 0.10, 0.50, make_metrics(cagr=0.01, max_dd=-0.40)),  # 回撤最轻
    ]
    view = select_by_constraint(results, max_dd=0.30, sort_by="cagr")
    assert not view.has_solution
    assert view.closest is not None and view.closest.combo == 1


def test_no_solution_message_is_absent_when_feasible_exists():
    view = select_by_constraint(grid_fixture(), max_dd=0.99)
    assert view.no_solution_message() is None


# ---------------------------------------------------------------------------
# 3. 输入校验与话术
# ---------------------------------------------------------------------------


def test_select_requires_train_metrics():
    no_train = SweepResult(
        combo=0,
        params={"dte_target": 30, "delta_target": 0.20, "profit_target_pct": 0.50},
        params_label="x",
        segments=SegmentMetrics(full=make_metrics(), train=None, test=None, train_end=None),
        run_dir=None,
    )
    with pytest.raises(ValueError, match="split.train_end"):
        select_by_constraint([no_train], max_dd=0.30)


@pytest.mark.parametrize("bad", [30.0, 1.0, 0.0, -0.3])
def test_select_rejects_non_fraction_thresholds(bad):
    with pytest.raises(ValueError, match="小数"):
        select_by_constraint(grid_fixture(), max_dd=bad)


def test_select_rejects_unknown_sort_field():
    with pytest.raises(ValueError, match="sort_by"):
        select_by_constraint(grid_fixture(), sort_by="not_a_metric")


def test_selection_disclaimer_states_no_prediction_principle():
    """Spec §17.2：筛选结果不得被表述为未来最优参数。"""
    assert "只使用 train 段" in SELECTION_DISCLAIMER
    assert "不等于未来最优参数" in SELECTION_DISCLAIMER
    assert "Spec §17.2" in SELECTION_DISCLAIMER


def test_selection_view_reports_filtered_counts():
    view = select_by_constraint(grid_fixture(), max_dd=0.30)
    assert (view.total, len(view.feasible), view.n_filtered_out) == (4, 1, 3)
