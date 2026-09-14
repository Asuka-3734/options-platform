"""M1-C：研究报告生成器（Spec §15.7 第 8 步；AC-10 / AC-14 / AC-15）。

从 sweep 输出目录（必要时加上最终评估目录）生成两份**离线自包含**报告：
- `report.html`：DTE×Delta 热图（≥4 指标）+ 边际 + 约束筛选表 + 分桶表 + 双基准 + 声明；
- `research_report.md`：同样的内容，Markdown 版（便于粘贴/存档/作品集）。

必须出现的内容（Spec §15 AC-10 / AC-14）：
假设清单（§17.1）· train/test 窗口 · 约束可行性 · Top-N · 敏感性观察 · §17.3 效度声明 ·
合成链**月度到期结构限制**声明 · **实际入场 DTE** 分桶 · test 统计噪声声明 · 双基准（§6.4）。

用法（在 options-platform 目录下）：
    uv run python scripts/build_sweep_report.py --sweep runs/sweep-official \\
        --final runs/sweep-official/final --max-mdd 0.30 \\
        --out-html runs/sweep-official/report.html \\
        --out-md runs/sweep-official/research_report.md
"""

from __future__ import annotations

import argparse
import csv
import html
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sellput.analysis import (
    BucketStats,
    entry_dte_bucket_stats,
)
from sellput.instruments import (
    EquitySpec,
    OptionRight,
    OptionSpec,
    OptionStyle,
    Settlement,
)
from sellput.portfolio import Trade
from sellput.research import (
    SELECTION_DISCLAIMER,
    SensitivityGrid,
    load_manifest,
    load_sweep_manifest,
    load_sweep_results,
    select_by_constraint,
    sensitivity_grid,
)
from sellput.vol import (
    bucket_by_rv,
    realized_vol,
    rolling_percentile,
    rv_bucket_stats,
)

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

HEATMAP_METRICS: tuple[tuple[str, str, str], ...] = (
    ("total_return", "训练段总收益", "pct"),
    ("cagr", "训练段 CAGR", "pct"),
    ("max_dd", "训练段最大回撤", "pct"),
    ("win_rate", "训练段胜率", "pct"),
    ("sharpe", "训练段 Sharpe", "num"),
    ("profit_factor", "训练段 Profit Factor", "num"),
)

ASSUMPTIONS: tuple[tuple[str, str, str], ...] = (
    ("1", "标的 = 真实 SPY 未复权日线（缓存/yfinance/CSV）", "realistic"),
    ("2", "期权报价 = 合成链（BS 定价）", "synthetic"),
    ("3", "合成链 iv_atm = 20d 滚动已实现波动率（t−1 口径）；无波动率风险溢价", "synthetic"),
    ("4", "合成链 skew = 0（默认）、价差 5bps 常数；**月度到期结构**", "synthetic"),
    ("5", "SPY 定价 = CRR 200 步美式；BS 仅作近似", "simplified"),
    ("6", "IV 反解 = BS 口径（美式标的亦然）", "simplified"),
    ("7", "分红 = 连续 q（过去 12 个月实际分红 ÷ 前收）", "simplified"),
    ("8", "成交 = Open(t) mid ± 5bps + $0.65/合约 + $1/单", "simplified"),
    ("9", "保证金 = Simplified Reg-T-style（ETF 20%）", "simplified（research approximation）"),
    ("10", "指派 = 仅到期规则（ITM 收盘指派），无提前指派", "simplified"),
    ("11", "指派后次日开盘卖出", "simplified"),
    ("12", "无风险利率恒定 4%；现金不计息", "simplified"),
    ("13", "仓位 = 可用资金 50% ÷ 单合约保证金，≤1 腿", "strategy/research choice"),
    ("14", "无 Stop Loss / Roll（M1 范围）", "strategy choice（研究假设，M2 验证）"),
    ("15", "保证金政策 = reject（**不模拟追保强平**，亏损可超过 100% 权益）", "simplified"),
    ("16", "基准 = 价格型 + 总回报型 B&H（分红次一交易日开盘再投，无摩擦无税）", "simplified"),
)

VALIDITY_STATEMENTS: tuple[str, ...] = (
    "本报告结论仅在 §17.1 假设与 synthetic 期权报价下成立。",
    "期权报价为合成（波动率 = SPY 20d 已实现波动率），非真实历史期权价格；收益与风险数字为"
    "研究近似，不构成任何未来表现预测。",
    "合成链为月度到期结构（M1-C 决策），因此参数敏感性按**实际入场 DTE** 分桶解释，"
    "而非假定 target DTE 等于实际 DTE。",
    "参数在 train 段上选择，test 段仅作样本外验证；test 样本有限，指标含统计噪声。",
    "参数搜索结果不是未来预测（Spec §17.2）：它只表示「在当前模型、数据与研究区间下，"
    "训练集内表现最好」。",
)


# ---------------------------------------------------------------------------
# 内容模型
# ---------------------------------------------------------------------------


@dataclass
class Paragraph:
    text: str


@dataclass
class Bullets:
    items: list[str]


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]


@dataclass
class Heatmap:
    title: str
    x_label: str
    y_label: str
    x_values: list[str]
    y_values: list[str]
    values: list[list[float | None]]
    fmt: str = "pct"
    marginals: list[tuple[str, list[str], list[float | None]]] = field(default_factory=list)


@dataclass
class Notice:
    text: str
    level: str = "info"  # info | warn


@dataclass
class Code:
    text: str


@dataclass
class Section:
    title: str
    blocks: list[object] = field(default_factory=list)


def fmt_value(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value * 100:+.2f}%"
    return f"{value:.3f}"


def fmt_plain(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value * 100:.2f}%"
    return f"{value:.2f}"


def pct_or_dash(value: float | None, *, digits: int = 2, signed: bool = True) -> str:
    """百分比格式化（None → —；可选正负号）。"""
    if value is None:
        return "—"
    sign = "+" if signed else ""
    return f"{value * 100:{sign}.{digits}f}%"


def num_or_dash(value: float | None, *, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

_BAD = (214, 96, 77)
_GOOD = (76, 153, 92)
_BAR_CHARS = "▁▂▃▄▅▆▇█"


def _lerp_color(t: float) -> str:
    r = int(_BAD[0] + (_GOOD[0] - _BAD[0]) * t)
    g = int(_BAD[1] + (_GOOD[1] - _BAD[1]) * t)
    b = int(_BAD[2] + (_GOOD[2] - _BAD[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _heat_colors(values: list[list[float | None]]) -> list[list[str | None]]:
    flat = [v for row in values for v in row if v is not None]
    if not flat:
        return [[None for _ in row] for row in values]
    lo, hi = min(flat), max(flat)
    span = hi - lo
    out: list[list[str | None]] = []
    for row in values:
        colors: list[str | None] = []
        for value in row:
            if value is None:
                colors.append(None)
            elif span == 0:
                colors.append(_lerp_color(0.5))
            else:
                colors.append(_lerp_color((value - lo) / span))
        out.append(colors)
    return out


def _bar(value: float | None, lo: float, hi: float) -> str:
    if value is None:
        return "—"
    if hi <= lo:
        return _BAR_CHARS[len(_BAR_CHARS) // 2]
    t = (value - lo) / (hi - lo)
    idx = min(len(_BAR_CHARS) - 1, max(0, int(round(t * (len(_BAR_CHARS) - 1)))))
    return _BAR_CHARS[idx] * 10


def render_html(sections: list[Section], *, title: str, subtitle: str) -> str:
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:24px 32px;color:#1f2933;"
        "line-height:1.6;max-width:1500px}",
        "h1{font-size:24px;margin-bottom:4px}h2{font-size:19px;margin-top:32px;"
        "border-bottom:2px solid #e4e7eb;padding-bottom:6px}h3{font-size:15px;margin-top:20px}",
        ".sub{color:#616e7c;margin-bottom:18px}",
        "table{border-collapse:collapse;margin:10px 0;font-size:13px}",
        "th,td{border:1px solid #cbd2d9;padding:4px 8px;text-align:right;white-space:nowrap}",
        "th{background:#f5f7fa;text-align:center}td.l,th.l{text-align:left}",
        ".notice{background:#eef4ff;border-left:4px solid #4c6ef5;padding:10px 14px;margin:12px 0}",
        ".warn{background:#fff4e5;border-left:4px solid #e8590c;padding:10px 14px;margin:12px 0}",
        ".meta{color:#616e7c;font-size:13px}",
        "code{background:#f5f7fa;padding:1px 4px;border-radius:3px}",
        "ul{margin:8px 0 8px 18px}",
        "</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f'<div class="sub">{html.escape(subtitle)}</div>',
    ]
    for section in sections:
        parts.append(f"<h2>{html.escape(section.title)}</h2>")
        for block in section.blocks:
            if isinstance(block, Paragraph):
                parts.append(f"<p>{html.escape(block.text)}</p>")
            elif isinstance(block, Bullets):
                items = "".join(f"<li>{html.escape(i)}</li>" for i in block.items)
                parts.append(f"<ul>{items}</ul>")
            elif isinstance(block, Notice):
                cls = "warn" if block.level == "warn" else "notice"
                parts.append(f'<div class="{cls}">{html.escape(block.text)}</div>')
            elif isinstance(block, Code):
                parts.append(f"<pre>{html.escape(block.text)}</pre>")
            elif isinstance(block, Table):
                head = "".join(
                    f'<th class="{"l" if i == 0 else ""}">{html.escape(h)}</th>'
                    for i, h in enumerate(block.headers)
                )
                body = "".join(
                    "<tr>"
                    + "".join(
                        f'<td class="{"l" if i == 0 else ""}">{html.escape(cell)}</td>'
                        for i, cell in enumerate(row)
                    )
                    + "</tr>"
                    for row in block.rows
                )
                parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            elif isinstance(block, Heatmap):
                colors = _heat_colors(block.values)
                parts.append(f"<h3>{html.escape(block.title)}</h3>")
                axis = f"{html.escape(block.y_label)} ＼ {html.escape(block.x_label)}"
                head = f'<th class="l">{axis}</th>'
                head += "".join(f"<th>{html.escape(x)}</th>" for x in block.x_values)
                body_rows = []
                for i, y in enumerate(block.y_values):
                    cells = "".join(
                        f'<td style="background:{colors[i][j] or "#ffffff"}">'
                        f"{html.escape(fmt_value(block.values[i][j], block.fmt))}</td>"
                        for j in range(len(block.x_values))
                    )
                    body_rows.append(f'<tr><th class="l">{html.escape(y)}</th>{cells}</tr>')
                parts.append(
                    f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
                )
                for label, labels, values in block.marginals:
                    flat = [v for v in values if v is not None]
                    lo, hi = (min(flat), max(flat)) if flat else (0.0, 1.0)
                    rows = "".join(
                        f'<tr><td class="l">{html.escape(name)}</td>'
                        f"<td>{html.escape(fmt_value(val, block.fmt))}</td>"
                        f'<td class="l"><code>{_bar(val, lo, hi)}</code></td></tr>'
                        for name, val in zip(labels, values, strict=True)
                    )
                    parts.append(
                        f"<p class='meta'>{html.escape(label)}（1D 边际）</p>"
                        f"<table><thead><tr><th class='l'>取值</th><th>平均</th>"
                        f"<th class='l'>相对水平</th></tr></thead><tbody>{rows}</tbody></table>"
                    )
    parts.append("</body></html>")
    return "\n".join(parts)


def render_markdown(sections: list[Section], *, title: str, subtitle: str) -> str:
    parts: list[str] = [f"# {title}", "", f"*{subtitle}*", ""]
    for section in sections:
        parts.append(f"## {section.title}")
        parts.append("")
        for block in section.blocks:
            if isinstance(block, Paragraph):
                parts.append(block.text)
                parts.append("")
            elif isinstance(block, Bullets):
                parts.extend(f"- {item}" for item in block.items)
                parts.append("")
            elif isinstance(block, Notice):
                parts.append(f"> {'⚠ ' if block.level == 'warn' else ''}{block.text}")
                parts.append("")
            elif isinstance(block, Code):
                parts.append("```bash")
                parts.append(block.text)
                parts.append("```")
                parts.append("")
            elif isinstance(block, Table):
                parts.append("| " + " | ".join(block.headers) + " |")
                parts.append("|" + "---|" * len(block.headers))
                for row in block.rows:
                    parts.append("| " + " | ".join(row) + " |")
                parts.append("")
            elif isinstance(block, Heatmap):
                parts.append(f"### {block.title}")
                parts.append("")
                axis = f"{block.y_label} ＼ {block.x_label}"
                parts.append(f"| {axis} | " + " | ".join(block.x_values) + " |")
                parts.append("|" + "---|" * (len(block.x_values) + 1))
                for i, y in enumerate(block.y_values):
                    cells = " | ".join(fmt_value(v, block.fmt) for v in block.values[i])
                    parts.append(f"| {y} | {cells} |")
                parts.append("")
                for label, labels, values in block.marginals:
                    flat = [v for v in values if v is not None]
                    lo, hi = (min(flat), max(flat)) if flat else (0.0, 1.0)
                    parts.append(f"**{label}（1D 边际）**")
                    parts.append("")
                    parts.append("| 取值 | 平均 | 相对水平 |")
                    parts.append("|---|---|---|")
                    for name, val in zip(labels, values, strict=True):
                        bar = _bar(val, lo, hi)
                        parts.append(f"| {name} | {fmt_value(val, block.fmt)} | `{bar}` |")
                    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 数据装配
# ---------------------------------------------------------------------------


def load_trades_csv(path: Path) -> list[Trade]:
    trades: list[Trade] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            kind = row.get("asset_kind") or "option"
            if kind == "equity":
                spec: OptionSpec | EquitySpec = EquitySpec(symbol=row.get("symbol") or "SPY")
            else:
                spec = OptionSpec(
                    underlying=row.get("symbol") or "SPY",
                    expiry=date.fromisoformat(row["expiry"]),
                    strike=float(row["strike"]),
                    right=OptionRight.PUT,
                    style=OptionStyle.AMERICAN,
                    settlement=Settlement.PHYSICAL,
                )
            trades.append(
                Trade(
                    id=int(row["id"]),
                    asset_kind=kind,
                    spec=spec,
                    qty=int(float(row["qty"])),
                    entry_date=date.fromisoformat(row["entry_date"]),
                    exit_date=date.fromisoformat(row["exit_date"]),
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["exit_price"]),
                    pnl=float(row["pnl"]),
                    commissions=float(row["commissions"]),
                    exit_reason=row["exit_reason"],
                    dte_at_entry=int(row["dte_at_entry"]) if row.get("dte_at_entry") else None,
                )
            )
    return trades


def load_prices_csv(path: Path) -> tuple[list[date], list[float]]:
    dates: list[date] = []
    closes: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            dates.append(date.fromisoformat(row["Date"]))
            closes.append(float(row["Close"]))
    return dates, closes


def bucket_table(stats: tuple[BucketStats, ...], first_header: str) -> Table:
    rows = []
    for stat in stats:
        rows.append(
            [
                stat.bucket,
                f"{stat.n_trades}",
                "—" if stat.win_rate is None else f"{stat.win_rate * 100:.1f}%",
                "—" if stat.avg_pnl is None else f"{stat.avg_pnl:,.0f}",
            ]
        )
    return Table(headers=[first_header, "笔数", "胜率", "平均 PnL"], rows=rows)


def build_sections(
    sweep_dir: Path,
    *,
    final_dir: Path | None,
    max_mdd: float | None,
    tp: float | None,
    top_n: int,
) -> tuple[list[Section], str, str]:
    sweep_manifest = load_sweep_manifest(sweep_dir)
    results = load_sweep_results(sweep_dir)
    grid = sweep_manifest.get("grid", {})
    sections: list[Section] = []

    data_cfg = sweep_manifest.get("base_config", {}).get("data", {})
    title = "Sell Put 参数研究报告（M1-C）"
    subtitle = (
        f"{sweep_manifest.get('sweep_id')} · 标的 {data_cfg.get('symbol')} · "
        f"{data_cfg.get('start')} ~ {data_cfg.get('end')} · "
        f"{sweep_manifest.get('n_combos')} 个组合（train ≤ {sweep_manifest.get('train_end')}）"
    )

    # 0. 运行信息
    code_version = (
        f"{sweep_manifest.get('git_commit')}（dirty={sweep_manifest.get('git_dirty')}）"
        f" · package {sweep_manifest.get('package_version')}"
        f" · python {sweep_manifest.get('python_version')}"
    )
    meta = Table(
        headers=["项目", "值"],
        rows=[
            ["sweep id", str(sweep_manifest.get("sweep_id"))],
            ["创建时间（UTC）", str(sweep_manifest.get("created_at"))],
            ["组合数 / 进程数",
             f"{sweep_manifest.get('n_combos')} / {sweep_manifest.get('workers')}"],
            ["搜索模式",
             f"{sweep_manifest.get('search_mode')}"
             f"（test_eval_count_total={sweep_manifest.get('test_eval_count_total')}）"],
            ["网格", f"DTE {grid.get('dte_target')} × delta {grid.get('delta_target')} "
                     f"× TP {grid.get('profit_target_pct')}"],
            ["train_end", str(sweep_manifest.get("train_end"))],
            ["数据指纹", str(sweep_manifest.get("dataset_fingerprint"))[:31] + "…"],
            ["代码版本", code_version],
        ],
    )
    symbols = data_cfg.get("symbol")
    start, end = data_cfg.get("start"), data_cfg.get("end")
    train_end = sweep_manifest.get("train_end")
    grid_dte = ",".join(str(v) for v in grid.get("dte_target", []))
    grid_delta = ",".join(f"{float(v):.2f}" for v in grid.get("delta_target", []))
    grid_tp = ",".join(f"{float(v):.2f}" for v in grid.get("profit_target_pct", []))
    final_params = "（未提供最终评估）"
    if final_dir is not None and (final_dir / "manifest.json").exists():
        final_manifest = load_manifest(final_dir)
        fp = final_manifest.get("params") or {}
        final_params = (
            f"--dte {fp.get('dte_target')} --delta {fp.get('delta_target')} "
            f"--tp {fp.get('profit_target_pct')}"
        )
    sweep_path = sweep_dir.as_posix()
    final_path = f"{sweep_path}/final"
    reproduce = "\n".join(
        [
            "# 1) 参数搜索（只看 train；不计算任何 test 指标）",
            f"uv run python scripts/run_sweep.py --dte {grid_dte} --delta {grid_delta} "
            f"--tp {grid_tp} --symbol {symbols} --start {start} --end {end} "
            f"--train-end {train_end} --offline --workers 8 --out {sweep_path}",
            "",
            "# 2) 最终样本外评估（唯一一次；双基准 + 全量制品）",
            f"uv run python scripts/finalize_experiment.py {final_params} --symbol {symbols} "
            f"--start {start} --end {end} --train-end {train_end} --offline "
            f"--out {final_path}",
            "",
            "# 3) 生成本报告（只读产物，不重跑引擎）",
            f"uv run python scripts/build_sweep_report.py --sweep {sweep_path} "
            f"--final {final_path} --max-mdd 0.30 "
            f"--out-html {sweep_path}/report.html --out-md {sweep_path}/research_report.md",
        ]
    )
    sections.append(
        Section(
            "0. 运行信息与可复现性",
            [
                meta,
                Paragraph("复现命令（在 options-platform 目录下按序执行）："),
                Code(reproduce),
                Paragraph(
                    "本报告由 scripts/build_sweep_report.py 自动生成，全部数字来自 sweep 产物"
                    "（每组合 manifest）与最终评估目录，未重新运行引擎。"
                ),
            ],
        )
    )

    # 1. 研究问题与窗口
    sections.append(
        Section(
            "1. 研究问题与数据窗口",
            [
                Bullets(
                    [
                        "研究问题：Sell Put 的 DTE / Delta / Take Profit 如何影响收益与风险"
                        "（收益、CAGR、最大回撤、风险调整后表现、胜率、尾部）。",
                        f"train：起始 ~ {sweep_manifest.get('train_end')}"
                        "（参数搜索只用这一段）；test：其后至期末"
                        "（仅用于最终样本外评估，Spec §15 F5 / D14）。",
                        f"参数网格：DTE {grid.get('dte_target')} "
                        f"× delta {grid.get('delta_target')} "
                        f"× TP {[f'{float(t):.0%}' for t in grid.get('profit_target_pct', [])]}。",
                        "口径：交易按平仓日归属分段；test 段以 train 末净值为起点；"
                        "搜索阶段不计算任何 test 指标。",
                    ]
                ),
                Notice(
                    "合成链到期结构限制：期权链只有**月度到期**（第三周五），因此目标 DTE 与实际"
                    "入场 DTE 会相差约 ±10 个交易日。参数敏感性必须按**实际入场 DTE** 解释"
                    "（见 §6 分桶表），不得假定 target DTE 等于实际 DTE（Spec §15 F3 / AC-10）。",
                    level="warn",
                ),
            ],
        )
    )

    # 2. 敏感性热图
    blocks: list[object] = [
        Paragraph(
            "热图为 DTE×Delta 的训练段指标（固定 TP 切片，见各图标题）；"
            "颜色越绿表示该指标数值越高（回撤为负值，越高即越轻）。"
        )
    ]
    tps = sorted({round(float(r.params["profit_target_pct"]), 10) for r in results})
    tp_value = round(float(tp), 10) if tp is not None else tps[0]
    for metric, label, kind in HEATMAP_METRICS:
        grid_data: SensitivityGrid = sensitivity_grid(results, metric=metric, tp=tp_value)
        heat = Heatmap(
            title=f"{label}（TP {tp_value:.0%}）",
            x_label="delta",
            y_label="DTE",
            x_values=[f"{d:.2f}" for d in grid_data.delta_values],
            y_values=[f"{int(d)}" for d in grid_data.dte_values],
            values=[list(row) for row in grid_data.values],
            fmt=kind,
            marginals=[
                (
                    "按 DTE 平均（跨 delta）",
                    [f"DTE {int(d)}" for d in grid_data.dte_values],
                    list(grid_data.marginal_dte()),
                ),
                (
                    "按 delta 平均（跨 DTE）",
                    [f"delta {d:.2f}" for d in grid_data.delta_values],
                    list(grid_data.marginal_delta()),
                ),
            ],
        )
        blocks.append(heat)
    sections.append(Section("2. 参数敏感性（DTE × Delta，训练段）", blocks))

    # 3. 敏感性观察（自动、事实性）
    cagr_grid = sensitivity_grid(results, metric="cagr", tp=tp_value)
    mdd_grid = sensitivity_grid(results, metric="max_dd", tp=tp_value)
    best_cagr = max(results, key=lambda r: (r.segments.train.cagr or float("-inf")))
    best_mdd = max(results, key=lambda r: r.segments.train.max_dd)
    slice_results = [
        r for r in results if round(float(r.params["profit_target_pct"]), 10) == tp_value
    ]
    best_in_slice = max(slice_results, key=lambda r: (r.segments.train.cagr or float("-inf")))
    wiped_out = [r for r in results if r.segments.train.total_return <= -1.0]
    delta_marginal = cagr_grid.marginal_delta()
    dte_marginal = cagr_grid.marginal_dte()
    observations = [
        f"固定 TP {tp_value:.0%} 切片内，训练段 CAGR 最高的组合是 {best_in_slice.params_label}"
        f"（CAGR {best_in_slice.segments.train.cagr * 100:+.2f}%、"
        f"MDD {best_in_slice.segments.train.max_dd * 100:.2f}%）。",
        f"全部 {len(results)} 个组合中训练段 CAGR 最高的是 {best_cagr.params_label}"
        f"（CAGR {best_cagr.segments.train.cagr * 100:+.2f}%、"
        f"MDD {best_cagr.segments.train.max_dd * 100:.2f}%）。",
        f"训练段回撤最轻的组合是 {best_mdd.params_label}"
        f"（MDD {best_mdd.segments.train.max_dd * 100:.2f}%）。",
        f"**{len(wiped_out)} 个组合在训练段把账户打到负权益**（总收益 ≤ −100%："
        + "、".join(r.params_label for r in wiped_out[:6])
        + ("…" if len(wiped_out) > 6 else "")
        + "）——这是「未模拟追保强平」（margin_policy=reject）下的模型产物，见表 §8 假设 15。",
        "按 delta 平均的 CAGR："
        + "、".join(
            f"{d:.2f} → {'—' if v is None else f'{v * 100:+.2f}%'}"
            for d, v in zip(cagr_grid.delta_values, delta_marginal, strict=True)
        )
        + "。",
        "按 DTE 平均的 CAGR："
        + "、".join(
            f"{int(d)} → {'—' if v is None else f'{v * 100:+.2f}%'}"
            for d, v in zip(cagr_grid.dte_values, dte_marginal, strict=True)
        )
        + "。",
        f"训练段 MDD 分布：全网格 [{min(r.segments.train.max_dd for r in results) * 100:.2f}%, "
        f"{max(r.segments.train.max_dd for r in results) * 100:.2f}%]；"
        f"TP {tp_value:.0%} 切片 "
        f"[{min(v for row in mdd_grid.values for v in row if v is not None) * 100:.2f}%, "
        f"{max(v for row in mdd_grid.values for v in row if v is not None) * 100:.2f}%]。",
        "以上仅为**训练段内的描述性观察**，不构成参数推荐，也不预示样本外表现（Spec §17.2）。",
    ]
    sections.append(Section("3. 敏感性观察（训练段，描述性）", [Bullets(observations)]))

    # 4. 约束筛选
    view = select_by_constraint(results, max_dd=max_mdd, sort_by="cagr")
    constraint_blocks: list[object] = [
        Paragraph(
            f"约束：{view.constraint_text()}；排序：{view.sort_by} 降序；"
            f"全部组合 {view.total}，满足约束 {len(view.feasible)}，被过滤 {view.n_filtered_out}。"
        )
    ]
    if max_mdd is not None and not view.has_solution:
        constraint_blocks.append(Notice(view.no_solution_message() or "", level="warn"))
    elif view.feasible:
        constraint_blocks.append(
            Table(
                headers=["组合", "参数", "train CAGR", "train MDD", "train 胜率", "笔数"],
                rows=[
                    [
                        f"#{r.combo}",
                        r.params_label,
                        pct_or_dash(r.segments.train.cagr),
                        f"{r.segments.train.max_dd * 100:.2f}%",
                        pct_or_dash(r.segments.train.win_rate, digits=1, signed=False),
                        f"{r.segments.train.n_trades}",
                    ]
                    for r in view.feasible[:top_n]
                ],
            )
        )
        if len(view.feasible) > top_n:
            constraint_blocks.append(
                Paragraph(f"（仅显示前 {top_n} 行，完整清单见 sweep_metrics.csv）")
            )
    constraint_blocks.append(Notice(SELECTION_DISCLAIMER))
    sections.append(Section("4. 约束筛选（训练段）", constraint_blocks))

    # 5. Top-N
    top = sorted(results, key=lambda r: (r.segments.train.cagr is None,
                                        -(r.segments.train.cagr or 0.0)))[:top_n]
    sections.append(
        Section(
            "5. Top-N（按训练段 CAGR 排序）",
            [
                Table(
                    headers=["#", "参数", "train 总收益", "train CAGR", "train MDD",
                             "train Sharpe", "train 胜率", "train PF", "笔数"],
                    rows=[
                        [
                            f"#{r.combo}",
                            r.params_label,
                            pct_or_dash(r.segments.train.total_return),
                            pct_or_dash(r.segments.train.cagr),
                            f"{r.segments.train.max_dd * 100:.2f}%",
                            num_or_dash(r.segments.train.sharpe),
                            pct_or_dash(r.segments.train.win_rate, digits=1, signed=False),
                            num_or_dash(r.segments.train.profit_factor),
                            f"{r.segments.train.n_trades}",
                        ]
                        for r in top
                    ],
                ),
                Notice("Top-N 是训练段排序结果，不是未来最优参数（Spec §17.2）。"),
            ],
        )
    )

    # 6. 最终评估（样本外 + 双基准 + 分桶）
    if final_dir is not None and (final_dir / "manifest.json").exists():
        sections.extend(_final_sections(final_dir))
    else:
        sections.append(
            Section(
                "6. 最终样本外评估",
                [
                    Notice(
                        "尚未提供最终评估目录（--final）：本报告暂不含 test 段结果、"
                        "RV / 实际入场 DTE 分桶与双基准对比。请先运行 "
                        "scripts/finalize_experiment.py 选定参数后再生成报告。",
                        level="warn",
                    )
                ],
            )
        )

    # 7. 假设清单 + 效度声明
    # 7. 研究结论（在当前假设下）
    conclusion: list[str] = []
    if max_mdd is not None and not view.has_solution:
        conclusion.append(
            f"**约束可行性**：在 {view.total} 个组合内没有任何一个满足 {view.constraint_text()}；"
            f"该参数网格内风险可行的参数**不存在**（最小回撤也有 "
            f"{view.closest.segments.train.max_dd * 100:.2f}%），"
            "因此本研究的正确结论是「不可行」，而不是「最优参数是 X」。"
        )
    else:
        conclusion.append(
            f"在 {view.total} 个组合中有 {len(view.feasible)} 个满足 {view.constraint_text()}。"
        )
    best_dte_idx = dte_marginal.index(max(dte_marginal))
    worst_dte_idx = dte_marginal.index(min(dte_marginal))
    conclusion.append(
        f"**参数方向（训练段描述性）**：按 delta 平均的 CAGR 从 {pct_or_dash(delta_marginal[0])} "
        f"（δ={cagr_grid.delta_values[0]:.2f}）变化到 {pct_or_dash(delta_marginal[-1])} "
        f"（δ={cagr_grid.delta_values[-1]:.2f}）；按 DTE 平均的 CAGR 在 "
        f"{pct_or_dash(max(dte_marginal))}（DTE {cagr_grid.dte_values[best_dte_idx]}）"
        f" 与 {pct_or_dash(min(dte_marginal))}"
        f"（DTE {cagr_grid.dte_values[worst_dte_idx]}）之间。"
        "高胜率并不对应高收益（见 §3）：收益差异来自少数大额亏损。"
    )
    if final_dir is not None and (final_dir / "manifest.json").exists():
        final_manifest = load_manifest(final_dir)
        f_metrics = final_manifest["metrics"]
        f_test = f_metrics.get("test") or {}
        f_full = f_metrics.get("full") or {}
        conclusion.append(
            "**样本外（唯一一次 test 评估）**：参数 "
            f"{final_manifest.get('params_label')} 的 test 段收益 "
            f"{pct_or_dash(f_test.get('total_return'))}，同期基准（价格型）"
            f"{pct_or_dash(f_test.get('benchmark_price_return'))}、基准（总回报型）"
            f"{pct_or_dash(f_test.get('benchmark_total_return'))}；"
            f"test 段最大回撤 {pct_or_dash(f_test.get('max_dd'), signed=False)}，"
            f"最差单笔 {f_test.get('worst_trade_pnl', 0):,.0f}。"
            f"全窗口对总回报基准的超额收益为 {pct_or_dash(f_full.get('excess_vs_total'))}。"
        )
    conclusion.append(
        "**对研究问题的回答**：DTE / Delta / Take Profit 三个维度**不足以**把尾部风险压到"
        "温和水平（见 §3 与 §6）；它们主要改变收益幅度与交易频率，而不是改变「少数交易决定"
        "成败」这一风险结构。是否可以通过止损 / 滚动换仓改变这一点，属于 M2 的第一批研究问题"
        "（Spec §16.1），本报告**不预设结论**。"
    )
    conclusion.append(
        "**效度**：以上结论全部在 §8 假设（尤其 synthetic 期权报价、月度到期结构、"
        "未模拟追保强平）下成立，不构成任何未来表现预测（Spec §17.2）。"
    )
    sections.append(
        Section(
            "7. 研究结论（在当前模型与数据假设下）",
            [Bullets(conclusion), Notice(SELECTION_DISCLAIMER)],
        )
    )

    sections.append(
        Section(
            "8. 金融假设登记表（Spec §17.1，与 Spec v0.7 同步）",
            [
                Table(
                    headers=["#", "假设", "标注"],
                    rows=[[a, b, c] for a, b, c in ASSUMPTIONS],
                )
            ],
        )
    )
    sections.append(
        Section(
            "9. 效度声明（Spec §17.3）",
            [
                Bullets(list(VALIDITY_STATEMENTS)),
                Notice(
                    "test 段统计噪声：样本外区间有限、交易笔数与极端事件样本少，"
                    "因此 test 指标（尤其尾部与回撤）本身带有明显抽样噪声，"
                    "不可据此宣称长期结论（Spec §15 F5 / AC-14）。",
                    level="warn",
                ),
            ],
        )
    )
    return sections, title, subtitle


def _final_sections(final_dir: Path) -> list[Section]:
    manifest = load_manifest(final_dir)
    metrics = manifest["metrics"]
    full, train, test = metrics.get("full"), metrics.get("train"), metrics.get("test")
    sections: list[Section] = []

    def row(label: str, data: dict | None) -> list[str]:
        if data is None:
            return [label, "—", "—", "—", "—", "—", "—", "—", "—", "—"]
        cagr = "—" if data["cagr"] is None else f"{data['cagr'] * 100:+.2f}%"
        vol = "—" if data["ann_vol"] is None else f"{data['ann_vol'] * 100:.2f}%"
        sharpe = "—" if data["sharpe"] is None else f"{data['sharpe']:.3f}"
        win = "—" if data["win_rate"] is None else f"{data['win_rate'] * 100:.1f}%"
        bp = data["benchmark_price_return"]
        bt = data["benchmark_total_return"]
        return [
            label,
            f"{data['total_return'] * 100:+.2f}%",
            cagr,
            vol,
            sharpe,
            f"{data['max_dd'] * 100:.2f}%",
            f"{data['dd_duration_days']}",
            f"{data['n_trades']}",
            win,
            f"{data['worst_trade_pnl']:,.0f}" if data["worst_trade_pnl"] is not None else "—",
            "—" if bp is None else f"{bp * 100:+.2f}%",
            "—" if bt is None else f"{bt * 100:+.2f}%",
        ]

    headers = ["段", "总收益", "CAGR", "年化波动", "Sharpe", "最大回撤",
               "回撤天数", "笔数", "胜率", "最差单笔", "基准(价格)", "基准(总回报)"]
    blocks: list[object] = [
        Paragraph(
            f"最终评估目录：`{final_dir.name}`；参数：{manifest.get('params_label')}"
            f"（test_eval_count={manifest.get('test_eval_count')}）。"
        ),
        Table(headers=headers, rows=[row("train", train), row("test", test), row("full", full)]),
    ]
    full_data = full or {}
    blocks.append(
        Notice(
            "双基准（Spec §6.4）：价格型 = 期末收盘 ÷ 期初收盘 − 1；"
            "总回报型 = 叠加现金分红再投资（除息日分红 ÷ 次一交易日开盘价，无摩擦无税）。"
            f"全窗口超额收益：对价格型 {full_data.get('excess_vs_price', 0) * 100:+.2f}%、"
            f"对总回报型 "
            + (
                "—（数据源无分红记录，退化为价格型）"
                if full_data.get("excess_vs_total") is None
                else f"{full_data['excess_vs_total'] * 100:+.2f}%"
            )
            + "。"
        )
    )

    trades_path = final_dir / "trades.csv"
    prices_path = final_dir / "prices.csv"
    if trades_path.exists():
        trades = load_trades_csv(trades_path)
        option_trades = [t for t in trades if t.asset_kind == "option"]
        blocks.append(
            Paragraph(
                f"最终评估共 {len(trades)} 笔成交（其中期权 {len(option_trades)} 笔）；"
                "下面按**实际入场 DTE** 与入场日 RV 分位分桶（合成链为月度到期结构）。"
            )
        )
        blocks.append(bucket_table(entry_dte_bucket_stats(trades), "实际入场 DTE"))
        if prices_path.exists():
            days, closes = load_prices_csv(prices_path)
            rv = realized_vol(closes, window=20)
            pct = rolling_percentile(rv, window=252, min_obs=20)
            buckets = bucket_by_rv(days, pct)  # shift=1：入场决策时点已知，防前视
            rv_stats = rv_bucket_stats(option_trades, buckets)
            blocks.append(
                Paragraph(
                    "RV 四桶（252d 分位；按**入场决策时点已知**的 20d 已实现波动率归桶，"
                    "与引擎 Close(t−1)→Open(t) 口径一致）："
                )
            )
            blocks.append(bucket_table(rv_stats, "RV 分位桶"))
            unbucketed = len(option_trades) - sum(s.n_trades for s in rv_stats)
            if unbucketed:
                blocks.append(Paragraph(f"（{unbucketed} 笔期权交易因波动率窗口预热无法归桶）"))
    sections.append(Section("6. 最终样本外评估、双基准与分桶", blocks))
    return sections


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M1-C 研究报告生成（HTML + Markdown）")
    ap.add_argument("--sweep", required=True, help="sweep 输出目录（含 sweep_manifest.json）")
    ap.add_argument("--final", default=None, help="最终评估目录（含 manifest.json / trades.csv）")
    ap.add_argument("--max-mdd", type=float, default=None,
                    help="约束筛选阈值（小数，如 0.30）；不填则只排序不过滤")
    ap.add_argument("--tp", type=float, default=None, help="热图使用的 TP 切片（默认取网格第一个）")
    ap.add_argument("--top", type=int, default=15, help="Top-N 行数（默认 15）")
    ap.add_argument("--out-html", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args(argv)

    sweep_dir = Path(args.sweep)
    final_dir = Path(args.final) if args.final else None
    try:
        sections, title, subtitle = build_sections(
            sweep_dir, final_dir=final_dir, max_mdd=args.max_mdd, tp=args.tp, top_n=args.top
        )
    except (FileNotFoundError, ValueError) as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    html_path = Path(args.out_html)
    md_path = Path(args.out_md)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(sections, title=title, subtitle=subtitle), encoding="utf-8")
    md_path.write_text(render_markdown(sections, title=title, subtitle=subtitle), encoding="utf-8")

    print(f"HTML 报告：{html_path}（{html_path.stat().st_size / 1024:.1f} KB，离线自包含）")
    print(f"Markdown 报告：{md_path}（{md_path.stat().st_size / 1024:.1f} KB）")
    print(f"章节：{' / '.join(s.title for s in sections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
