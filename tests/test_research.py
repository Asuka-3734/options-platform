"""M1-C 步骤 4：`research` 研究层测试（Spec v0.7 §15 F5 / §15.5 / AC-5）。

覆盖：
1. 分段指标口径（train/test/full、test 段以 train 末净值为起点、交易按 entry_date 归属）；
2. 单组合运行封装（provider 复用、`include_test_metrics=False` 结构性隐藏 test）；
3. **AC-5 结构性隔离**：篡改 test 段数据 → train 段指标逐位不变 → train 阶段选出的参数不变；
4. 复现性（同配置两次运行分段指标一致）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from sellput.config import (
    BacktestConfig,
    DataConfig,
    SellPutParams,
    SplitConfig,
    StrategyConfig,
    SyntheticConfig,
)
from sellput.instruments import NyseCalendar, OptionRight, OptionSpec, OptionStyle, Settlement
from sellput.market_data import MarketSnapshot, OptionQuote, build_provider
from sellput.portfolio import PortfolioState, Trade
from sellput.research import ResearchRun, run_once, segment_metrics

TRAIN_END = date(2020, 7, 1)
DATA_START = date(2020, 1, 1)
DATA_END = date(2020, 12, 31)

GRID: list[dict] = [
    {"dte_target": 20, "delta_target": 0.15},
    {"dte_target": 20, "delta_target": 0.25},
    {"dte_target": 30, "delta_target": 0.15},
    {"dte_target": 30, "delta_target": 0.25},
]


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def make_states(equity: list[float], *, start: date = date(2020, 1, 1)) -> list[PortfolioState]:
    out: list[PortfolioState] = []
    for i, value in enumerate(equity):
        out.append(
            PortfolioState(
                date=start + timedelta(days=i),
                cash=value,
                positions=(),
                positions_value=0.0,
                margin_used=0.0,
                equity=value,
                greeks={},
                attribution={},
                benchmark_price=value,
            )
        )
    return out


def make_trade(
    trade_id: int, entry_day: date, pnl: float, *, exit_day: date | None = None
) -> Trade:
    return Trade(
        id=trade_id,
        asset_kind="option",
        spec=OptionSpec(
            underlying="SPX",
            expiry=entry_day + timedelta(days=30),
            strike=3000.0,
            right=OptionRight.PUT,
            style=OptionStyle.EUROPEAN,
            settlement=Settlement.CASH,
        ),
        qty=-1,
        entry_date=entry_day,
        exit_date=exit_day or entry_day + timedelta(days=20),
        entry_price=5.0,
        exit_price=0.0,
        pnl=pnl,
        commissions=0.0,
        exit_reason="expire",
    )


def make_config(
    params: dict,
    *,
    train_end: date | None = TRAIN_END,
    start: date = DATA_START,
    end: date = DATA_END,
) -> BacktestConfig:
    """合成数据 + SPX（BS 定价，跑得快）的小窗口配置；用于结构性测试。"""
    return BacktestConfig(
        data=DataConfig(
            provider="synthetic",
            symbol="SPX",
            start=start,
            end=end,
            synthetic=SyntheticConfig(
                seed=11, s0=3000.0, sigma=0.20, iv_atm=0.20, strikes_per_side=4, strike_span=0.10
            ),
        ),
        strategy=StrategyConfig(type="sell_put", params=SellPutParams(**params)),
        split=SplitConfig(train_end=train_end),
    )


class PostSplitMutationProvider:
    """只篡改 ``train_end`` 之后快照的测试替身（把期权报价整体抬高一档）。

    train 段快照原样透传，因此可用于验证"test 段数据不影响 train 段结论"（AC-5）。

    只放大报价、不动标的价格：若同时放大标的价格，深度实值 Put 会跌破无套利下界
    （`implied_vol` 会正确报错），那是测试替身自身的构造缺陷，而非引擎问题。
    """

    def __init__(self, inner, train_end: date, *, factor: float = 1.25) -> None:
        self._inner = inner
        self._train_end = train_end
        self._factor = factor

    def __getattr__(self, name: str):
        """其余属性（`dividend_yield_at` / `q` / `kind` 等）原样委托给 inner。

        必须委托：`build_dividend_model` 通过 `hasattr(provider, "dividend_yield_at")` 选择
        按日股息率还是常数股息率；替身若"少一个属性"，base 与 mutated 会用**不同**的股息率模型，
        两边就不再可比（且可能触发无套利下界误报）。
        """
        return getattr(self._inner, name)

    def sessions(self) -> list[date]:
        return self._inner.sessions()

    def expiries_available(self, day: date) -> list[date]:
        return self._inner.expiries_available(day)

    def close_snapshot(self, day, extra_strikes=None) -> MarketSnapshot:
        return self._mutate(day, self._inner.close_snapshot(day, extra_strikes))

    def open_snapshot(self, day, extra_strikes=None) -> MarketSnapshot:
        return self._mutate(day, self._inner.open_snapshot(day, extra_strikes))

    def _mutate(self, day: date, snap: MarketSnapshot) -> MarketSnapshot:
        if day <= self._train_end:
            return snap
        f = self._factor
        quotes = {
            oid: OptionQuote(
                bid=quote.bid * f,
                ask=quote.ask * f,
                last=quote.last * f,
                volume=quote.volume,
                open_interest=quote.open_interest,
            )
            for oid, quote in snap.option_quotes.items()
        }
        return replace(snap, option_quotes=quotes)


def _select_by_train_cagr(runs: list[tuple[dict, ResearchRun]]) -> dict:
    """测试用最简选择规则：train 段 CAGR 最大（正式筛选工具见 §15.7 第 7 步）。"""
    return max(runs, key=lambda item: item[1].segments.train.cagr or float("-inf"))[0]


# ---------------------------------------------------------------------------
# 1. 分段指标口径（手算）
# ---------------------------------------------------------------------------


def test_segment_metrics_hand_computed():
    """+10% 复利路径：train 0.21、test 0.331（起点 = train 末净值），复合回到 full 0.61051。"""
    equity = [100_000.0 * 1.1**i for i in range(6)]
    states = make_states(equity)
    trades = [
        make_trade(1, date(2020, 1, 2), 100.0, exit_day=date(2020, 1, 3)),  # 开平都在 train
        make_trade(2, date(2020, 1, 2), -50.0, exit_day=date(2020, 1, 4)),  # 跨切分日 → test
        make_trade(3, date(2020, 1, 5), 300.0, exit_day=date(2020, 1, 6)),  # test
    ]
    seg = segment_metrics(
        states, trades, starting_cash=100_000.0, train_end=date(2020, 1, 3)
    )

    assert seg.train_end == date(2020, 1, 3)
    assert seg.full.n_sessions == 6 and seg.train.n_sessions == 3 and seg.test.n_sessions == 3
    assert seg.train.total_return == pytest.approx(0.21, rel=1e-12)
    assert seg.test.total_return == pytest.approx(0.331, rel=1e-12)
    assert seg.full.total_return == pytest.approx(0.61051, rel=1e-12)
    # 复合一致性：(1+train)(1+test) - 1 == full
    assert (1 + seg.train.total_return) * (1 + seg.test.total_return) - 1 == pytest.approx(
        seg.full.total_return, rel=1e-12
    )
    # 交易按 exit_date 归属：仅"开平都在 train 段"的计入 train
    assert (seg.train.n_trades, seg.test.n_trades, seg.full.n_trades) == (1, 2, 3)
    assert seg.train.start == date(2020, 1, 1) and seg.train.end == date(2020, 1, 3)
    assert seg.test.start == date(2020, 1, 4) and seg.test.end == date(2020, 1, 6)


def test_segment_metrics_without_split_has_only_full():
    states = make_states([100_000.0, 101_000.0])
    seg = segment_metrics(states, [], starting_cash=100_000.0, train_end=None)
    assert seg.train is None and seg.test is None and seg.train_end is None
    assert seg.full.n_sessions == 2


def test_segment_metrics_empty_segments_are_none():
    states = make_states([100_000.0, 101_000.0, 102_000.0])
    before = segment_metrics(states, [], starting_cash=100_000.0, train_end=date(2019, 1, 1))
    assert before.train is None  # train 段无状态
    assert before.test is not None and before.test.n_sessions == 3

    after = segment_metrics(states, [], starting_cash=100_000.0, train_end=date(2021, 1, 1))
    assert after.test is None  # test 段无状态
    assert after.train is not None and after.train.n_sessions == 3


def test_segment_metrics_can_hide_test_structurally():
    """`include_test_metrics=False`（搜索模式）：连 test 指标都不计算（Spec §15 F5）。"""
    states = make_states([100_000.0, 101_000.0, 102_000.0, 103_000.0])
    seg = segment_metrics(
        states, [], starting_cash=100_000.0, train_end=date(2020, 1, 2), include_test_metrics=False
    )
    assert seg.train is not None
    assert seg.test is None


def test_trade_crossing_split_counts_as_test():
    """跨切分日的交易（train 开仓 / test 平仓）计入 test 段——train 段统计不得含 test 数据。"""
    states = make_states([100_000.0, 101_000.0, 102_000.0])
    crossing = make_trade(1, date(2020, 1, 1), 500.0, exit_day=date(2020, 1, 5))
    seg = segment_metrics(states, [crossing], starting_cash=100_000.0, train_end=date(2020, 1, 1))
    assert seg.train.n_trades == 0
    assert seg.test.n_trades == 1


# ---------------------------------------------------------------------------
# 2. 单组合运行封装
# ---------------------------------------------------------------------------


def test_run_once_produces_segments_and_keeps_provider():
    cfg = make_config(GRID[0])
    run = run_once(cfg)
    assert isinstance(run, ResearchRun)
    assert run.config is cfg
    assert run.provider is not None  # 供 RV 分桶/价格导出复用
    assert run.segments.full.n_sessions == len(run.result.states)
    assert run.segments.train.n_sessions + run.segments.test.n_sessions == len(run.result.states)
    # 交易日历生效：2020-01-01（元旦）不是交易日，首日为 01-02
    assert run.segments.train.start == run.result.states[0].date == date(2020, 1, 2)
    assert run.segments.train.end == TRAIN_END
    assert run.segments.test.start > TRAIN_END
    assert run.segments.test.end == run.result.states[-1].date
    assert run.result.trades  # 窗口内应有成交


def test_run_once_hides_test_metrics_in_search_mode():
    run = run_once(make_config(GRID[0]), include_test_metrics=False)
    assert run.segments.train is not None
    assert run.segments.test is None


def test_run_once_is_reproducible():
    cfg = make_config(GRID[1])
    a = run_once(cfg).segments
    b = run_once(cfg).segments
    assert a.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# 3. AC-5：test 段数据不影响 train 阶段的选择（结构性隔离）
# ---------------------------------------------------------------------------


def test_test_segment_mutation_does_not_change_train_metrics_or_selection():
    """篡改 test 段数据（train 段原样）：
    ① train 段指标逐位不变；② 由 train 指标得出的选择不变；③ 篡改确实生效（test/full 变化）。
    """
    calendar = NyseCalendar()
    base_provider = build_provider(make_config(GRID[0]))
    mutated_provider = PostSplitMutationProvider(base_provider, TRAIN_END)

    runs_a: list[tuple[dict, ResearchRun]] = []
    runs_b: list[tuple[dict, ResearchRun]] = []
    for params in GRID:
        cfg = make_config(params)
        runs_a.append((params, run_once(cfg, provider=base_provider, calendar=calendar)))
        runs_b.append((params, run_once(cfg, provider=mutated_provider, calendar=calendar)))

    # ① train 段指标逐位一致
    for (params_a, run_a), (params_b, run_b) in zip(runs_a, runs_b, strict=True):
        assert params_a == params_b
        assert run_a.segments.train == run_b.segments.train
        assert run_a.segments.train.to_dict() == run_b.segments.train.to_dict()

    # ② train 阶段选出的参数不受 test 段篡改影响
    assert _select_by_train_cagr(runs_a) == _select_by_train_cagr(runs_b)

    # ③ 篡改确实改变了样本外与全窗口结果（否则本测试是空的）
    assert any(
        run_a.segments.test != run_b.segments.test
        for (_, run_a), (_, run_b) in zip(runs_a, runs_b, strict=True)
    )
    assert any(
        run_a.segments.full != run_b.segments.full
        for (_, run_a), (_, run_b) in zip(runs_a, runs_b, strict=True)
    )


def test_search_mode_never_computes_test_metrics_across_grid():
    """搜索模式的网格运行：每条结果都不含 test 指标（Spec §15 F5 的工程保证）。"""
    calendar = NyseCalendar()
    provider = build_provider(make_config(GRID[0]))
    for params in GRID:
        run = run_once(
            make_config(params),
            provider=provider,
            calendar=calendar,
            include_test_metrics=False,
        )
        assert run.segments.test is None
        assert run.segments.train is not None
