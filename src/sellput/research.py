"""研究层：单组合运行与样本外切分（Spec §15 F5 / §15.5；M1-C 实施步骤 4）。

职责边界（Spec §15 核心原则）
------------------------------
- 只**重复调用**引擎（`build_provider` + `SimulationEngine`）：不修改引擎、不引入新的交易语义、
  不重算持仓会计。研究能力（sweep / 筛选 / 报告）全部建立在本模块之上。
- **参数搜索只允许读 train**（Spec §15 F5 / §10 D14）：`run_once(..., include_test_metrics=False)`
  **连 test 指标都不计算**，从结构上杜绝"偷看样本外"；test 只用于最终一次评估。
- 一次回测 = 一条连续净值曲线；test 段指标以 **train 末状态净值**为起点计算（不是"重开账户"），
  因此 train+test 的复合收益 == full 收益。

口径（写入报告，Spec §15 AC-14）
--------------------------------
- 分段切分点：`split.train_end`；`date <= train_end` 属 train，其后属 test（`None` = 不切分）。
- **交易归属按 `exit_date`**（平仓日）：只有**开平仓都落在 train 段内**的交易才计入 train 段统计。
  这是 AC-5「篡改 test 段数据不得改变 train 段的任何指标」的必要条件——若按开仓日归属，
  跨切分日平仓的交易会把 test 段的平仓价带进 train 段的 PnL 统计（profit_factor/avg_win 等）。
  因此跨切分日的交易计入 **test** 段，报告需明示这类交易的笔数。
- 无法归段（空段）时对应字段为 `None`（"未定义即 None"，与 `analysis` 模块一致）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import platform
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

from . import __version__
from .analysis import Metrics, compute_metrics, total_return_index
from .config import (
    BacktestConfig,
    BuyHoldParams,
    DataConfig,
    SellPutParams,
    StrategyConfig,
    SweepConfig,
)
from .instruments import NyseCalendar, OptionSpec, TradingCalendar
from .market_data import MarketDataProvider, build_provider
from .portfolio import PortfolioState, Trade
from .sim import SimResult, SimulationEngine


@dataclass(frozen=True, slots=True)
class SegmentMetrics:
    """三段指标（Spec §15.5）。

    `full` 在搜索阶段为 ``None``（sweep 只产出 train 指标，避免泄露样本外，Spec §15 F5）。
    """

    full: Metrics | None = None
    train: Metrics | None = None
    test: Metrics | None = None
    train_end: date | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "train_end": self.train_end.isoformat() if self.train_end is not None else None,
            "full": self.full.to_dict(),
            "train": self.train.to_dict() if self.train is not None else None,
            "test": self.test.to_dict() if self.test is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ResearchRun:
    """一次回测的研究层产出：配置 + 数据源 + 引擎结果 + 分段指标。

    `provider` 一并返回，供 RV 分桶（Spec §15 F7）与价格导出复用，避免重复加载数据。
    """

    config: BacktestConfig
    provider: MarketDataProvider
    result: SimResult
    segments: SegmentMetrics


def segment_metrics(
    states: Sequence[PortfolioState],
    trades: Sequence[Trade],
    *,
    starting_cash: float,
    train_end: date | None = None,
    risk_free_rate: float = 0.0,
    include_test_metrics: bool = True,
    benchmark_total_index: Sequence[float] | None = None,
) -> SegmentMetrics:
    """按 ``train_end`` 切分并计算三段指标（纯函数）。

    Args:
        states: 完整回测的逐日状态（连续净值曲线）。
        trades: 完整回测的交易清单。
        starting_cash: 回测期初资金。
        train_end: 切分日；``None`` = 不切分（只出 full）。
        risk_free_rate: 年化无风险利率（透传 `compute_metrics`）。
        include_test_metrics: ``False`` 时不计算 test 段指标（Spec §15 F5：搜索阶段禁看样本外）。
        benchmark_total_index: 与 `states` 等长的总回报基准指数（Spec §6.4）；按同一日期切分。
    """
    if benchmark_total_index is not None and len(benchmark_total_index) != len(states):
        raise ValueError("benchmark_total_index 必须与 states 等长")

    full_bench = list(benchmark_total_index) if benchmark_total_index is not None else None
    full = compute_metrics(
        states,
        trades,
        starting_cash=starting_cash,
        risk_free_rate=risk_free_rate,
        benchmark_total_index=full_bench,
    )
    if train_end is None:
        return SegmentMetrics(full=full, train=None, test=None, train_end=None)

    split_positions = [i for i, s in enumerate(states) if s.date <= train_end]
    train_states = [s for s in states if s.date <= train_end]
    test_states = [s for s in states if s.date > train_end]
    # 交易按平仓日归属：保证 train 段指标只依赖 train 段数据（Spec §15 AC-5）
    train_trades = [t for t in trades if t.exit_date <= train_end]
    test_trades = [t for t in trades if t.exit_date > train_end]
    train_bench = full_bench[: len(split_positions)] if full_bench is not None else None
    test_bench = full_bench[len(split_positions) :] if full_bench is not None else None

    train: Metrics | None = None
    if train_states:
        train = compute_metrics(
            train_states,
            train_trades,
            starting_cash=starting_cash,
            risk_free_rate=risk_free_rate,
            benchmark_total_index=train_bench,
        )

    test: Metrics | None = None
    if include_test_metrics and test_states:
        # test 段以 train 末状态净值为起点：连续曲线的口径，train×test 复合 == full
        test_start_cash = train_states[-1].equity if train_states else starting_cash
        test_bench_base = train_bench[-1] if train_bench else None
        test = compute_metrics(
            test_states,
            test_trades,
            starting_cash=test_start_cash,
            risk_free_rate=risk_free_rate,
            benchmark_total_index=test_bench,
            benchmark_total_base=test_bench_base,
        )

    return SegmentMetrics(full=full, train=train, test=test, train_end=train_end)


def total_return_benchmark(
    cfg: BacktestConfig, sessions: Sequence[date]
) -> list[float] | None:
    """构造总回报基准指数并对齐到 `sessions`（Spec §6.4 双基准）。

    - ``hybrid``：通过公开的 `load_price_series` 读取真实分红，按"除息日分红 ÷ 次一交易日开盘价"
      再投；若数据源没有分红记录 → 返回 ``None``（报告需显式声明"总回报基准退化为价格型"）。
    - ``synthetic``：合成数据不含分红记录 → ``None``。

    **不改引擎、不改 B&H 策略**：基准只发生在分析/报告层（Spec §6.4 最小设计原则 4）。
    """
    if cfg.data.provider != "hybrid":
        return None
    from .data import load_price_series  # 懒加载：纯合成路径不触碰数据层

    hybrid = cfg.data.hybrid
    series = load_price_series(
        symbol=cfg.data.symbol,
        start=cfg.data.start,
        end=cfg.data.end,
        cache_dir=hybrid.cache_dir,
        csv_path=hybrid.csv_path,
        buffer_days=hybrid.buffer_days,
        allow_network=not hybrid.offline,
    )
    if not series.dividends:
        return None
    index = total_return_index(
        dates=series.dates,
        opens=series.open,
        closes=series.close,
        dividends=series.dividends,
    )
    by_date = dict(zip(series.dates, index, strict=True))
    try:
        return [by_date[day] for day in sessions]
    except KeyError as err:  # 数据窗口/缓冲不足
        raise ValueError(f"总回报基准缺少交易日 {err}（检查数据起点与 buffer_days）") from err


def run_once(
    cfg: BacktestConfig,
    *,
    provider: MarketDataProvider | None = None,
    calendar: TradingCalendar | None = None,
    include_test_metrics: bool = True,
    include_total_return_benchmark: bool = False,
) -> ResearchRun:
    """单组合运行封装（Spec §15.7 第 4 步）：回测 + 三段指标。

    Args:
        cfg: 完整回测配置（含 `split.train_end` 与策略参数）。
        provider: 数据源；``None`` 时按 `cfg.data` 构建。测试可用替身注入（AC-5 结构性测试）。
        calendar: 交易日历；``None`` 时用 NYSE。批量运行应复用同一实例以省去重复预计算。
        include_test_metrics: ``True``（单次评估，默认）/ ``False``（参数搜索，禁看样本外）。
        include_total_return_benchmark: ``True`` 时计算**总回报基准**（分红再投，Spec §6.4），
            与价格型基准同时产出；数据源无分红记录时为 ``None``（需在报告中声明降级）。
    """
    prov = provider if provider is not None else build_provider(cfg)
    cal = calendar if calendar is not None else NyseCalendar()
    result = SimulationEngine(cfg).run(prov, cal)
    benchmark_index = None
    if include_total_return_benchmark:
        benchmark_index = total_return_benchmark(cfg, [s.date for s in result.states])
    segments = segment_metrics(
        result.states,
        result.trades,
        starting_cash=cfg.account.starting_cash,
        train_end=cfg.split.train_end,
        include_test_metrics=include_test_metrics,
        benchmark_total_index=benchmark_index,
    )
    return ResearchRun(config=cfg, provider=prov, result=result, segments=segments)


# ---------------------------------------------------------------------------
# 实验 manifest（Spec §15 F8 / §15.5；M1-C 实施步骤 5）
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
METRICS_FILENAME = "metrics.json"

#: 对比表里的数值列（取自 `Metrics`，不含 start/end 溯源字段）
TABLE_METRIC_FIELDS = (
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "calmar",
    "max_dd",
    "dd_duration_days",
    "n_trades",
    "win_rate",
    "profit_factor",
    "p5_trade_pnl",
    "worst_trade_pnl",
    "capital_efficiency",
    "benchmark_price_return",
    "excess_vs_price",
    "n_sessions",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _git_info(repo_root: str) -> tuple[str | None, str | None, bool | None]:
    """(commit, branch, dirty)；git 不可用或非仓库时返回 ``None``，绝不抛错。

    进程内只查询一次（lru_cache）：批量 sweep 不会反复 fork git。
    """

    def run(args: list[str]) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = run(["rev-parse", "HEAD"])
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    status = run(["status", "--porcelain"])
    dirty = None if status is None else bool(status)
    return commit, branch, dirty


def params_label(cfg: BacktestConfig) -> str:
    """策略参数的紧凑标签（run_id 与对比表用；精确值以 manifest 的 ``params`` 为准）。"""
    params = cfg.strategy.params
    if isinstance(params, SellPutParams):
        return (
            f"DTE {params.dte_target} / delta {params.delta_target:.2f} / "
            f"止盈 {params.profit_target_pct:.0%}"
        )
    if isinstance(params, BuyHoldParams):
        return f"Buy & Hold {params.allocation:.0%}"
    return str(params)


def default_run_id(cfg: BacktestConfig, *, now: datetime | None = None) -> str:
    """`YYYYmmdd-HHMMSS_<symbol>-<参数标签>`（本地时间）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    symbol = cfg.data.symbol.lower()
    params = cfg.strategy.params
    if isinstance(params, SellPutParams):
        delta_pct = int(round(params.delta_target * 100))
        tp_pct = int(round(params.profit_target_pct * 100))
        label = f"dte{params.dte_target}-delta{delta_pct:02d}-tp{tp_pct:02d}"
    elif isinstance(params, BuyHoldParams):
        label = "buyhold"
    else:
        label = "run"
    return f"{stamp}_{symbol}-{label}"


def dataset_fingerprint(states: Sequence[PortfolioState]) -> str:
    """数据指纹：对本次运行**实际使用**的标的收盘序列（日期 + 收盘价）取 sha256。

    与文件布局无关（缓存 CSV / yfinance / 手动 CSV 的同一份数据 → 同一指纹）；
    换窗口、换数据或数据被改动 → 指纹变化（Spec §15 N2 复现性 / AC-8）。
    """
    digest = hashlib.sha256()
    for state in states:
        digest.update(f"{state.date.isoformat()}|{state.benchmark_price:.10g};".encode())
    return "sha256:" + digest.hexdigest()


def build_manifest(
    run: ResearchRun,
    *,
    run_id: str | None = None,
    created_at: str | None = None,
    test_eval_count: int | None = None,
) -> dict[str, object]:
    """构建实验 manifest（Spec §15.5 字段 + 解释这些字段所必需的溯源信息）。

    `test_eval_count` 语义（Spec §15 F5 的审计信号）：本次运行**是否计算了 test 段指标**
    —— 0 = 搜索模式（`include_test_metrics=False`），1 = 最终样本外评估。搜索阶段出现非 0
    即说明有人偷看了样本外。
    """
    return manifest_from_parts(
        run.config,
        run.segments,
        fingerprint=dataset_fingerprint(run.result.states),
        run_id=run_id,
        created_at=created_at,
        test_eval_count=test_eval_count,
    )


def manifest_from_parts(
    cfg: BacktestConfig,
    segments: SegmentMetrics,
    *,
    fingerprint: str,
    run_id: str | None = None,
    created_at: str | None = None,
    test_eval_count: int | None = None,
) -> dict[str, object]:
    """由"配置 + 三段指标 + 数据指纹"构建 manifest（sweep 的 worker 不回传引擎对象，走此入口）。"""
    commit, branch, dirty = _git_info(str(_repo_root()))
    if test_eval_count is None:
        test_eval_count = 1 if segments.test is not None else 0
    metrics = {
        "full": segments.full.to_dict() if segments.full is not None else None,
        "train": segments.train.to_dict() if segments.train is not None else None,
        "test": segments.test.to_dict() if segments.test is not None else None,
    }
    window = segments.full or segments.train
    if window is None:  # pragma: no cover - 至少有一段指标才可能构建清单
        raise ValueError("manifest 需要至少一段指标（full 或 train）")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id if run_id is not None else default_run_id(cfg),
        "created_at": created_at
        if created_at is not None
        else datetime.now(UTC).isoformat(timespec="seconds"),
        # ---- Spec §15.5 ExperimentManifest 字段 ----
        "config": cfg.model_dump(mode="json"),
        "git_commit": commit,
        "package_version": __version__,
        "dataset_fingerprint": fingerprint,
        "train_end": segments.train_end.isoformat() if segments.train_end is not None else None,
        "test_eval_count": test_eval_count,
        "metrics": metrics,
        # ---- 附加溯源（用于解释上面的字段，非新功能）----
        "strategy": cfg.strategy.type,
        "params": cfg.strategy.params.model_dump(mode="json"),
        "params_label": params_label(cfg),
        "dataset": {
            "provider": cfg.data.provider,
            "symbol": cfg.data.symbol,
            "requested_start": cfg.data.start.isoformat(),
            "requested_end": cfg.data.end.isoformat(),
            "actual_start": window.start.isoformat(),
            "actual_end": window.end.isoformat(),
            "n_sessions": window.n_sessions,
        },
        "git_branch": branch,
        "git_dirty": dirty,
        "python_version": platform.python_version(),
    }


def write_manifest_json(manifest: dict, out_dir: str | Path) -> Path:
    """把已构建好的 manifest（+ metrics）写入实验目录。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / METRICS_FILENAME).write_text(
        json.dumps(manifest["metrics"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    path = out / MANIFEST_FILENAME
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_manifest(
    run: ResearchRun,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    created_at: str | None = None,
    test_eval_count: int | None = None,
) -> Path:
    """把 ``manifest.json`` 与 ``metrics.json`` 写入实验目录（Spec §4.3 / §15 F8）。"""
    return write_manifest_json(
        build_manifest(run, run_id=run_id, created_at=created_at, test_eval_count=test_eval_count),
        out_dir,
    )


def load_manifest(path: str | Path) -> dict:
    """读取 manifest：接受实验目录或 ``manifest.json`` 路径。"""
    target = Path(path)
    if target.is_dir():
        target = target / MANIFEST_FILENAME
    if not target.exists():
        raise FileNotFoundError(f"未找到 manifest：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def metrics_from_dict(data: dict) -> Metrics:
    """由 `Metrics.to_dict()` 的落盘结果还原 `Metrics`（日期字段转回 `date`）。"""
    payload = dict(data)
    payload["start"] = date.fromisoformat(payload["start"])
    payload["end"] = date.fromisoformat(payload["end"])
    return Metrics(**payload)


def load_sweep_manifest(out_dir: str | Path) -> dict:
    """读取 sweep 级清单（`sweep_manifest.json`）。"""
    path = Path(out_dir) / "sweep_manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"未找到 sweep 清单：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_sweep_results(out_dir: str | Path) -> list[SweepResult]:
    """从 sweep 输出目录读回各组合结果（Spec §15.7 第 8 步报告/后验视图所需）。

    只读取 train 指标：搜索阶段写下的 full/test 均为 ``None``（Spec §15 F5），
    因此读回的对象同样**不含样本外信息**。
    """
    out = Path(out_dir)
    results: list[SweepResult] = []
    combo_dirs = sorted(p for p in out.glob("combo-*") if (p / MANIFEST_FILENAME).exists())
    for combo, combo_dir in enumerate(combo_dirs):
        manifest = load_manifest(combo_dir)
        train_payload = (manifest.get("metrics") or {}).get("train")
        if train_payload is None:
            raise ValueError(f"{combo_dir} 缺少 train 指标（不是搜索阶段产出？）")
        train_end = manifest.get("train_end")
        results.append(
            SweepResult(
                combo=combo,
                params=dict(manifest.get("params") or {}),
                params_label=str(manifest.get("params_label") or ""),
                segments=SegmentMetrics(
                    full=None,  # 搜索阶段未写 full（含 test 段，Spec §15 F5）
                    train=metrics_from_dict(train_payload),
                    test=None,
                    train_end=date.fromisoformat(train_end) if train_end else None,
                ),
                run_dir=combo_dir.name,
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class SensitivityGrid:
    """二维参数敏感性网格（Spec §15 F3）：行 = DTE、列 = delta、值 = 指定指标。"""

    metric: str
    tp: float
    dte_values: tuple[int, ...]
    delta_values: tuple[float, ...]
    values: tuple[tuple[float | None, ...], ...]  # values[行=DTE][列=delta]

    def marginal_dte(self) -> tuple[float | None, ...]:
        """按 DTE 的边际（对同一 DTE 下所有 delta 取值求平均）——"1D 边际线"。"""
        return tuple(
            _mean_or_none([v for v in row if v is not None]) for row in self.values
        )

    def marginal_delta(self) -> tuple[float | None, ...]:
        """按 delta 的边际（对同一 delta 下所有 DTE 取值求平均）。"""
        out: list[float | None] = []
        for j in range(len(self.delta_values)):
            column = [row[j] for row in self.values if row[j] is not None]
            out.append(_mean_or_none([v for v in column if v is not None]))
        return tuple(out)


def _mean_or_none(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def sensitivity_grid(
    results: Sequence[SweepResult],
    *,
    metric: str,
    tp: float | None = None,
) -> SensitivityGrid:
    """抽取 DTE×delta 的敏感性网格（固定 TP 切片，Spec §15 F3）。

    ``tp`` 为 ``None`` 时取结果中出现的第一个 TP 值（报告需标注用的是哪个切片）。
    """
    if metric not in TABLE_METRIC_FIELDS:
        raise ValueError(f"metric 必须是 {TABLE_METRIC_FIELDS} 之一，收到 {metric!r}")
    if not results:
        raise ValueError("没有 sweep 结果")

    tps = sorted({round(float(r.params["profit_target_pct"]), 10) for r in results})
    tp_value = round(float(tp), 10) if tp is not None else tps[0]
    dtes = sorted({int(r.params["dte_target"]) for r in results})
    deltas = sorted({round(float(r.params["delta_target"]), 10) for r in results})

    table: dict[tuple[int, float], float | None] = {}
    for result in results:
        if round(float(result.params["profit_target_pct"]), 10) != tp_value:
            continue
        train = result.segments.train
        value = None if train is None else getattr(train, metric, None)
        key = (int(result.params["dte_target"]), round(float(result.params["delta_target"]), 10))
        table[key] = None if value is None else float(value)

    rows = tuple(
        tuple(table.get((dte, delta)) for delta in deltas) for dte in dtes
    )
    return SensitivityGrid(
        metric=metric,
        tp=tp_value,
        dte_values=tuple(dtes),
        delta_values=tuple(deltas),
        values=rows,
    )


def write_run_artifacts(
    run: ResearchRun,
    out_dir: str | Path,
    *,
    include_prices: bool = True,
) -> Path:
    """把一次运行的全量制品写入实验目录（Spec §4.3）。

    产出：manifest.json + metrics.json + states.csv + trades.csv（+ prices.csv）。

    与 sweep 的 metrics-first 相对：这是**最终评估**那一次运行的完整留档（Spec §15 N4 的
    `--save-all` 形态）。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = write_manifest(run, out, run_id=out.name)
    (out / "states.csv").write_text(_states_csv(run.result.states), encoding="utf-8")
    (out / "trades.csv").write_text(_trades_csv(run.result.trades), encoding="utf-8")
    if include_prices:
        prices = _prices_csv(run.provider)
        if prices:
            (out / "prices.csv").write_text(prices, encoding="utf-8")
    return manifest_path


def _short_fingerprint(fingerprint: object) -> str | None:
    """`sha256:abcdef…` → `sha256:abcdefghijkl`（对比表里只用于"是否同一份数据"）。"""
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    prefix, _, digest = fingerprint.partition(":")
    return f"{prefix}:{digest[:12]}" if digest else fingerprint[:12]


def comparison_table(
    manifests: Sequence[dict],
    *,
    segment: str = "train",
    sort_by: str | None = None,
) -> list[dict]:
    """把多个 manifest 合并成对比表（Spec §15 F8 / AC-8）。

    **D14 纪律（结构性保证）**：`segment="test"` 时禁止排序——样本外结果不得用于选参。
    """
    if segment not in ("train", "test", "full"):
        raise ValueError(f"unknown segment: {segment}")
    if segment == "test" and sort_by is not None:
        raise ValueError(
            "Spec §15 F8 / D14：test 段禁止排序 —— 不得按样本外结果选择参数；"
            "如需排序请用 --segment train"
        )
    if sort_by is not None and sort_by not in TABLE_METRIC_FIELDS:
        raise ValueError(f"sort_by 必须是 {TABLE_METRIC_FIELDS} 之一，收到 {sort_by!r}")

    rows: list[dict] = []
    for manifest in manifests:
        metrics = manifest.get("metrics") or {}
        segment_metrics = metrics.get(segment) or {}
        row: dict = {
            "run_id": manifest.get("run_id"),
            "strategy": manifest.get("strategy"),
            "params": manifest.get("params_label"),
            "train_end": manifest.get("train_end"),
            "git_commit": (manifest.get("git_commit") or "")[:7] or None,
            "data": _short_fingerprint(manifest.get("dataset_fingerprint")),
            "created_at": manifest.get("created_at"),
            "test_eval_count": manifest.get("test_eval_count"),
            "segment": segment,
        }
        row.update({field: segment_metrics.get(field) for field in TABLE_METRIC_FIELDS})
        rows.append(row)

    if sort_by is not None:
        rows.sort(
            key=lambda r: (
                r[sort_by] is None,
                -(r[sort_by] if r[sort_by] is not None else 0.0),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 网格 sweep（Spec §15 F2 / N4；M1-C 实施步骤 6）
# ---------------------------------------------------------------------------

DEFAULT_MAX_WORKERS = 8

#: 逐组合产出的全量制品（仅 `--save-all` 时写盘；默认 metrics-first，Spec §15 N4）
FULL_ARTIFACT_FILES = ("states.csv", "trades.csv", "prices.csv")

_WORKER_SAVE_ALL = False


@dataclass(frozen=True, slots=True)
class SweepResult:
    """单个参数组合的搜索结果（Spec §15.5）。"""

    combo: int
    params: dict[str, object]
    params_label: str
    segments: SegmentMetrics
    run_dir: str | None = None  # 相对 sweep 输出目录的路径（确定性，便于复现定位）


@dataclass(frozen=True, slots=True)
class SweepRun:
    """一次 sweep 的完整产出（网格 + 每组合结果 + 落盘路径）。"""

    sweep_id: str
    configs: tuple[BacktestConfig, ...]
    results: tuple[SweepResult, ...]
    metrics_csv: Path | None = None
    manifest_path: Path | None = None

    @property
    def n_combos(self) -> int:
        return len(self.results)

    def benchmark_returns(self) -> dict[str, float | None]:
        """基准（价格型 Buy & Hold）在 train/test/full 三段的收益；各组合相同，取首个组合。"""
        first = self.results[0].segments if self.results else None
        if first is None:
            return {"train": None, "test": None, "full": None}

        def value(metrics: Metrics | None) -> float | None:
            return metrics.benchmark_price_return if metrics is not None else None

        return {"train": value(first.train), "test": value(first.test), "full": value(first.full)}


def expand_sweep(cfg: BacktestConfig) -> list[BacktestConfig]:
    """把 ``cfg.sweep`` 的三个维度展开成笛卡尔积（Spec §15 F2）。

    顺序固定为 ``dte_target × delta_target × profit_target_pct``（各维度按配置给出的顺序）；
    空维度表示"不扫描该维度"，沿用基础配置的取值；三个维度都为空时退化为单次运行。
    """
    grid_dims = (cfg.sweep.dte_target, cfg.sweep.delta_target, cfg.sweep.profit_target_pct)
    if cfg.strategy.type != "sell_put":
        if any(grid_dims):
            raise ValueError(
                f"sweep 网格仅适用于 sell_put 策略（当前 strategy.type={cfg.strategy.type}）"
            )
        return [cfg.model_copy(deep=True)]

    base = cfg.strategy.params
    if not isinstance(base, SellPutParams):  # pragma: no cover - StrategyConfig 已保证类型
        raise TypeError("sell_put 策略的参数必须是 SellPutParams")

    dtes = cfg.sweep.dte_target or [base.dte_target]
    deltas = cfg.sweep.delta_target or [base.delta_target]
    tps = cfg.sweep.profit_target_pct or [base.profit_target_pct]

    configs: list[BacktestConfig] = []
    for dte in dtes:
        for delta in deltas:
            for tp in tps:
                params = SellPutParams(
                    dte_target=dte,
                    delta_target=delta,
                    profit_target_pct=tp,
                    dte_exit=base.dte_exit,
                    entry_frequency=base.entry_frequency,
                    max_open_positions=base.max_open_positions,
                )
                configs.append(
                    cfg.model_copy(
                        update={
                            "strategy": StrategyConfig(type="sell_put", params=params),
                            "sweep": SweepConfig(),  # 展开后的单点配置不再带网格
                        },
                        deep=True,
                    )
                )
    return configs


def combo_label(combo: int, params: dict[str, object]) -> str:
    """组合目录名（确定性、无时间戳）：`combo-000_dte30-delta20-tp50`。"""
    delta_pct = int(round(float(params["delta_target"]) * 100))
    tp_pct = int(round(float(params["profit_target_pct"]) * 100))
    return (
        f"combo-{combo:03d}_dte{params['dte_target']}"
        f"-delta{delta_pct:02d}-tp{tp_pct:02d}"
    )


@lru_cache(maxsize=4)
def _cached_provider(data_json: str, rate: float) -> MarketDataProvider:
    """按 `DataConfig` 缓存数据源：同一进程内多个组合复用（hybrid 只读一次 CSV）。"""
    cfg = BacktestConfig(data=DataConfig.model_validate_json(data_json), market={"rate": rate})
    return build_provider(cfg)


@lru_cache(maxsize=1)
def _cached_calendar() -> TradingCalendar:
    return NyseCalendar()


def _init_worker(save_all: bool) -> None:
    global _WORKER_SAVE_ALL
    _WORKER_SAVE_ALL = save_all


def _states_csv(states: Sequence[PortfolioState]) -> str:
    """与 `scripts/run_backtest.py` 同列的每日状态（含 equity 曲线，可直接用于报告）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date",
            "equity",
            "cash",
            "positions_value",
            "margin_used",
            "benchmark_close",
            "g_delta",
            "g_theta",
            "a_total",
            "a_delta",
            "a_theta",
            "a_vega",
            "a_residual",
        ]
    )
    for state in states:
        writer.writerow(
            [
                state.date.isoformat(),
                state.equity,
                state.cash,
                state.positions_value,
                state.margin_used,
                state.benchmark_price,
                state.greeks.get("delta", 0.0),
                state.greeks.get("theta", 0.0),
                state.attribution.get("total", 0.0),
                state.attribution.get("delta", 0.0),
                state.attribution.get("theta", 0.0),
                state.attribution.get("vega", 0.0),
                state.attribution.get("residual", 0.0),
            ]
        )
    return buffer.getvalue()


def _trades_csv(trades: Sequence[Trade]) -> str:
    """与 `scripts/run_backtest.py` 同列的逐笔交易。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "asset_kind",
            "symbol",
            "strike",
            "expiry",
            "qty",
            "entry_date",
            "exit_date",
            "entry_price",
            "exit_price",
            "pnl",
            "commissions",
            "exit_reason",
            "entry_iv",
            "exit_iv",
            "dte_at_entry",
            "delta_at_entry",
        ]
    )
    for trade in trades:
        spec = trade.spec
        is_option = isinstance(spec, OptionSpec)
        writer.writerow(
            [
                trade.id,
                trade.asset_kind,
                spec.underlying if is_option else spec.symbol,
                spec.strike if is_option else "",
                spec.expiry.isoformat() if is_option else "",
                trade.qty,
                trade.entry_date.isoformat(),
                trade.exit_date.isoformat(),
                trade.entry_price,
                trade.exit_price,
                trade.pnl,
                trade.commissions,
                trade.exit_reason,
                "" if trade.entry_iv is None else trade.entry_iv,
                "" if trade.exit_iv is None else trade.exit_iv,
                "" if trade.dte_at_entry is None else trade.dte_at_entry,
                "" if trade.delta_at_entry is None else trade.delta_at_entry,
            ]
        )
    return buffer.getvalue()


def _prices_csv(provider: MarketDataProvider) -> str:
    frame_writer = getattr(provider, "prices_frame", None)
    if frame_writer is None:
        return ""
    return frame_writer().to_csv(index=False)


def _sweep_worker(cfg: BacktestConfig) -> dict[str, object]:
    """搜索模式下的单组合执行（**不计算 test 指标**，Spec §15 F5/F2）。"""
    provider = _cached_provider(cfg.data.model_dump_json(), cfg.market.rate)
    result = SimulationEngine(cfg).run(provider, _cached_calendar())
    segments = segment_metrics(
        result.states,
        result.trades,
        starting_cash=cfg.account.starting_cash,
        train_end=cfg.split.train_end,
        include_test_metrics=False,  # 结构性禁看样本外
    )
    files: dict[str, str] = {}
    if _WORKER_SAVE_ALL:
        files["states.csv"] = _states_csv(result.states)
        files["trades.csv"] = _trades_csv(result.trades)
        prices = _prices_csv(provider)
        if prices:
            files["prices.csv"] = prices
    return {
        "segments": segments,
        "fingerprint": dataset_fingerprint(result.states),
        "files": files,
    }


def _metrics_row(
    combo: int,
    cfg: BacktestConfig,
    segments: SegmentMetrics,
    fingerprint: str,
    run_dir: str,
) -> dict:
    """`sweep_metrics.csv` 的一行：只含确定性字段（无时间戳/随机量）→ 两次运行逐字节一致。"""
    params = cfg.strategy.params
    row: dict = {
        "combo": combo,
        "run_dir": run_dir,
        "dte_target": params.dte_target,
        "delta_target": params.delta_target,
        "profit_target_pct": params.profit_target_pct,
        "params_label": params_label(cfg),
        "train_end": segments.train_end.isoformat() if segments.train_end is not None else "",
        "dataset_fingerprint": fingerprint,
        "git_commit": _git_info(str(_repo_root()))[0] or "",
        "package_version": __version__,
    }
    # 搜索阶段只输出 train 指标：full 窗口含 test 段，写出去就等于泄露样本外（Spec §15 F5）
    train = segments.train
    if train is not None:
        data = train.to_dict()
        for field in TABLE_METRIC_FIELDS:
            row[f"train_{field}"] = data.get(field)
    return row


def default_sweep_id(*, now: datetime | None = None) -> str:
    """`sweep-YYYYmmdd-HHMMSS`（本地时间；仅用于目录/清单标识，不参与指标 CSV）。"""
    return "sweep-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# 约束筛选视图（Spec §15 F4 / AC-6；M1-C 实施步骤 7）
# ---------------------------------------------------------------------------

INFEASIBLE_MESSAGE = "无可行解"

SELECTION_DISCLAIMER = (
    "筛选与排序只使用 train 段（Spec §15 F4 / F5、D14）；test 段未参与，"
    "也不得据 test 结果回头选参。结果表述为「在当前模型、数据与研究区间下，"
    "训练集内表现最好的参数」——它不等于未来最优参数（Spec §17.2）。"
)


@dataclass(frozen=True, slots=True)
class SelectionView:
    """MDD 约束下的筛选视图（Spec §15 F4 / AC-6）。

    **只读 train 段**：本类型与 `select_by_constraint` 都不接受 test/full 指标，
    从结构上不存在"用样本外选参"的入口。
    """

    sort_by: str
    max_dd_limit: float | None  # 阈值（小数，如 0.30 = MDD ≤ −30%）；None = 不设约束
    total: int
    feasible: tuple[SweepResult, ...]
    closest: SweepResult | None  # 全部组合中 MDD 最大者（最接近约束，但可能仍不满足）

    @property
    def has_solution(self) -> bool:
        return bool(self.feasible)

    @property
    def n_filtered_out(self) -> int:
        return self.total - len(self.feasible)

    def constraint_text(self) -> str:
        if self.max_dd_limit is None:
            return "无约束"
        return f"train 最大回撤 ≥ −{self.max_dd_limit:.2%}"

    def no_solution_message(self) -> str | None:
        """可行集为空时的显式说明（绝不用"最接近"冒充可行解）。"""
        if self.has_solution or self.max_dd_limit is None:
            return None
        lines = [
            f"{INFEASIBLE_MESSAGE}：{self.total} 个组合中没有任何一个满足 "
            f"{self.constraint_text()}。",
            "系统不会自动放宽阈值，也不会把「最接近」的组合当作可行解返回。",
        ]
        if self.closest is not None and self.closest.segments.train is not None:
            params = self.closest.params
            lines.append(
                "当前参数网格内最小回撤的组合是 "
                f"DTE {params['dte_target']} / delta {float(params['delta_target']):.2f} / "
                f"TP {float(params['profit_target_pct']):.0%}"
                f"（train MDD {self.closest.segments.train.max_dd:.2%}）"
                "——**最接近约束 ≠ 满足约束**。"
            )
        return "\n".join(lines)


def select_by_constraint(
    results: Sequence[SweepResult],
    *,
    max_dd: float | None = None,
    sort_by: str = "cagr",
) -> SelectionView:
    """在 train 段上按 MDD 约束筛选，并按指定指标降序排序（Spec §15 F4 / AC-6）。

    Args:
        results: sweep 结果（必须带 train 段指标）。
        max_dd: MDD 阈值（小数，如 ``0.30`` 表示"最大回撤不超过 30%"）；``None`` = 只排序不过滤。
        sort_by: 排序指标（`TABLE_METRIC_FIELDS` 之一），降序；缺失值排最后。
    """
    if sort_by not in TABLE_METRIC_FIELDS:
        raise ValueError(f"sort_by 必须是 {TABLE_METRIC_FIELDS} 之一，收到 {sort_by!r}")
    if max_dd is not None and not (0.0 < max_dd < 1.0):
        raise ValueError(f"max_dd 需为小数（如 0.30 表示 30%），收到 {max_dd!r}")
    if not results:
        return SelectionView(
            sort_by=sort_by, max_dd_limit=max_dd, total=0, feasible=(), closest=None
        )

    missing = [r.combo for r in results if r.segments.train is None]
    if missing:
        raise ValueError(
            "筛选需要 train 段指标（Spec §15 F5）：请在配置里设置 split.train_end；"
            f"缺 train 指标的组合：{missing[:5]}"
        )

    def metric_value(result: SweepResult) -> float | None:
        train = result.segments.train
        assert train is not None
        value = getattr(train, sort_by, None)
        return None if value is None else float(value)

    def sort_key(result: SweepResult) -> tuple[bool, float]:
        value = metric_value(result)
        return (value is None, -(value if value is not None else 0.0))

    ordered = sorted(results, key=sort_key)
    if max_dd is None:
        feasible = tuple(ordered)
    else:
        threshold = -abs(float(max_dd))
        feasible = tuple(
            r
            for r in ordered
            if (r.segments.train is not None and r.segments.train.max_dd >= threshold)
        )

    closest = max(
        results,
        key=lambda r: r.segments.train.max_dd if r.segments.train is not None else float("-inf"),
    )
    return SelectionView(
        sort_by=sort_by,
        max_dd_limit=max_dd,
        total=len(results),
        feasible=feasible,
        closest=closest,
    )


def sweep(
    cfg: BacktestConfig,
    *,
    out_dir: str | Path | None = None,
    workers: int | None = None,
    save_all: bool = False,
    sweep_id: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SweepRun:
    """网格 sweep（Spec §15 F2 / N4；M1-C 实施步骤 6）。

    - **搜索模式**：每个组合都**不计算 test 指标**（F5/D14 的结构性保证），
      因此 sweep 产出的 `test_eval_count_total` 恒为 0；样本外评估对最终选中的组合单独做。
    - **metrics-first**：默认只写每组合的 `manifest.json`/`metrics.json` 与 sweep 级
      `sweep_metrics.csv`/`sweep_manifest.json`；`save_all=True` 才额外写 states/trades/prices。
    - **确定性**：组合相互独立 + 引擎确定性 ⇒ 结果与网格大小、顺序、进程数无关；
      `sweep_metrics.csv` 只含确定性字段 ⇒ 同配置连跑两遍逐字节一致（N2 / AC-7）。
    - **失败即停**：任一组合抛错就中断并报出组合编号与参数（不做静默的部分 sweep）。
    """
    configs = expand_sweep(cfg)
    worker_count = max(
        1,
        int(workers if workers is not None else min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1)),
    )
    payloads: list[dict[str, object]] = []

    if worker_count == 1 or len(configs) == 1:
        _init_worker(save_all)
        for index, combo_cfg in enumerate(configs):
            payloads.append(_run_combo_checked(combo_cfg, index))
            if progress is not None:
                progress(index + 1, len(configs))
    else:
        payloads = [{} for _ in configs]
        with ProcessPoolExecutor(
            max_workers=worker_count, initializer=_init_worker, initargs=(save_all,)
        ) as pool:
            futures = {
                pool.submit(_sweep_worker, combo_cfg): i for i, combo_cfg in enumerate(configs)
            }
            completed = 0
            for future in as_completed(futures):
                index = futures[future]
                try:
                    payloads[index] = future.result()
                except Exception as err:  # noqa: BLE001 组合失败必须带上下文报出
                    raise RuntimeError(
                        f"sweep 组合 #{index}（{params_label(configs[index])}）执行失败：{err}"
                    ) from err
                completed += 1
                if progress is not None:
                    progress(completed, len(configs))

    results: list[SweepResult] = []
    rows: list[dict] = []
    for index, (combo_cfg, payload) in enumerate(zip(configs, payloads, strict=True)):
        segments = payload["segments"]
        params = combo_cfg.strategy.params
        assert isinstance(segments, SegmentMetrics) and isinstance(params, SellPutParams)
        name = combo_label(index, params.model_dump())
        run_dir = name if out_dir is not None else None
        results.append(
            SweepResult(
                combo=index,
                params=params.model_dump(),
                params_label=params_label(combo_cfg),
                segments=segments,
                run_dir=run_dir,
            )
        )
        assert isinstance(payload["fingerprint"], str)
        rows.append(
            _metrics_row(index, combo_cfg, segments, payload["fingerprint"], run_dir or "")
        )

    sweep_run = SweepRun(
        sweep_id=sweep_id if sweep_id is not None else default_sweep_id(),
        configs=tuple(configs),
        results=tuple(results),
    )

    if out_dir is not None:
        metrics_csv, manifest_path = _write_sweep_outputs(
            sweep_run, out_dir, configs=configs, payloads=payloads, workers=worker_count,
            save_all=save_all, rows=rows, base_cfg=cfg,
        )
        sweep_run = SweepRun(
            sweep_id=sweep_run.sweep_id,
            configs=sweep_run.configs,
            results=sweep_run.results,
            metrics_csv=metrics_csv,
            manifest_path=manifest_path,
        )
    return sweep_run


def _run_combo_checked(combo_cfg: BacktestConfig, index: int) -> dict[str, object]:
    try:
        return _sweep_worker(combo_cfg)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(
            f"sweep 组合 #{index}（{params_label(combo_cfg)}）执行失败：{err}"
        ) from err


def _write_sweep_outputs(
    sweep_run: SweepRun,
    out_dir: str | Path,
    *,
    configs: Sequence[BacktestConfig],
    payloads: Sequence[dict[str, object]],
    workers: int,
    save_all: bool,
    rows: Sequence[dict],
    base_cfg: BacktestConfig,
) -> tuple[Path, Path]:
    """写 sweep 级清单/指标 CSV + 每组合的 manifest（`save_all` 时附全量制品，Spec §15 N4）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    combo_entries: list[dict] = []
    for index, (combo_cfg, payload, result) in enumerate(
        zip(configs, payloads, sweep_run.results, strict=True)
    ):
        name = result.run_dir or combo_label(index, combo_cfg.strategy.params.model_dump())
        combo_dir = out / name
        manifest = manifest_from_parts(
            combo_cfg,
            result.segments,
            fingerprint=str(payload["fingerprint"]),
            run_id=name,
            test_eval_count=0,  # 搜索模式：从未计算 test 指标
        )
        # 搜索阶段的清单只保留 train 指标：full 窗口含 test 段，写出去就等于泄露样本外
        # （Spec §15 F5 / D14：参数选择只能看 train）
        manifest["metrics"]["full"] = None
        manifest["metrics"]["test"] = None
        write_manifest_json(manifest, combo_dir)
        files = payload.get("files") or {}
        assert isinstance(files, dict)
        for filename, text in files.items():
            (combo_dir / filename).write_text(str(text), encoding="utf-8")
        combo_entries.append(
            {"combo": index, "run_dir": name, "params": combo_cfg.strategy.params.model_dump()}
        )

    metrics_csv = out / "sweep_metrics.csv"
    fieldnames = list(rows[0]) if rows else ["combo"]
    with metrics_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    commit, branch, dirty = _git_info(str(_repo_root()))
    sweep_manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "sweep_id": sweep_run.sweep_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_config": base_cfg.model_dump(mode="json"),
        "grid": {
            "dte_target": base_cfg.sweep.dte_target or [base_cfg.strategy.params.dte_target],
            "delta_target": base_cfg.sweep.delta_target or [base_cfg.strategy.params.delta_target],
            "profit_target_pct": base_cfg.sweep.profit_target_pct
            or [base_cfg.strategy.params.profit_target_pct],
        },
        "n_combos": sweep_run.n_combos,
        "workers": workers,
        "save_all": save_all,
        "search_mode": True,
        "test_eval_count_total": 0,  # 搜索阶段未计算任何 test 指标（Spec §15 F5）
        "train_end": payloads[0]["segments"].train_end.isoformat() if payloads else None,
        "dataset_fingerprint": str(payloads[0]["fingerprint"]) if payloads else None,
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "package_version": __version__,
        "python_version": platform.python_version(),
        "metrics_csv": metrics_csv.name,
        "combos": combo_entries,
    }
    manifest_path = out / "sweep_manifest.json"
    manifest_path.write_text(
        json.dumps(sweep_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics_csv, manifest_path
