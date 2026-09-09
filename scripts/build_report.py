"""M1-A 回测可视化报告生成器：只读 experiments/m1a/ 的 CSV，重新计算并输出自包含 HTML。

- 不运行回测引擎、不修改任何回测逻辑；图表数据全部来自已有导出文件。
- 生成 report.html 无任何外部依赖（无 CDN），浏览器本地双击即可打开。
- 用法（在 options-platform 目录下）：
      uv run python scripts/build_report.py                 # 默认读 experiments/m1a
      uv run python scripts/build_report.py --indir DIR --out out.html
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

START_CASH = 100_000.0

# 与 run_backtest.py 的 REASON_ZH 保持一致
REASON_ZH = {
    "take_profit": "止盈 50%",
    "dte_exit": "DTE ≤ 3 强退",
}

# 背景标注的历史市场阶段（仅作阴影标注，非策略信号）
PHASES = [
    ("2008 金融危机", "2007-10-09", "2009-03-09"),
    ("COVID-19 疫情崩盘", "2020-02-19", "2020-03-23"),
    ("2022 加息熊市", "2022-01-03", "2022-10-12"),
]


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def fmt_pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def explain(rows: list[str]) -> str:
    body = "".join(f"<li>{r}</li>" for r in rows)
    return f"<h3>人话解读</h3><ul>{body}</ul>"


def load_states(path: Path) -> tuple[list[str], list[float], list[float], list[float]]:
    dates: list[str] = []
    eq: list[float] = []
    bench: list[float] = []
    pos: list[float] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dates.append(row["date"])
            eq.append(round(float(row["equity"]), 2))
            bench.append(round(float(row["benchmark_close"]), 4))
            pos.append(round(float(row["positions_value"]), 2))
    return dates, eq, bench, pos


def load_trades(path: Path) -> list[dict]:
    trades: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            entry = row["entry_date"]
            exit_ = row["exit_date"]
            trades.append(
                {
                    "id": int(row["id"]),
                    "strike": float(row["strike"]),
                    "expiry": row["expiry"],
                    "qty": int(round(abs(float(row["qty"])))),
                    "entry_date": entry,
                    "exit_date": exit_,
                    "pnl": round(float(row["pnl"]), 2),
                    "exit_reason": row["exit_reason"],
                    "entry_iv": round(float(row["entry_iv"]), 4),
                    "delta_at_entry": round(float(row["delta_at_entry"]), 3),
                    "hold": (date.fromisoformat(exit_) - date.fromisoformat(entry)).days,
                }
            )
    return trades


def load_prices(path: Path, t0: str, t1: str) -> tuple[list[str], list[float]]:
    dates: list[str] = []
    closes: list[float] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row["Date"]
            if t0 <= d <= t1:  # 截掉两端预热缓冲，只看回测窗口
                dates.append(d)
                closes.append(round(float(row["Close"]), 2))
    return dates, closes


def drawdowns(values: list[float]) -> list[float]:
    peak = -1e18
    out: list[float] = []
    for v in values:
        peak = max(peak, v)
        out.append(round(v / peak - 1.0, 6) if peak > 0 else 0.0)
    return out


def histogram(pnls: list[float]) -> list[dict]:
    bins = [{"lo": lo, "hi": lo + 500, "n": 0} for lo in range(-10_000, 10_000, 500)]
    overflow = 0
    for p in pnls:
        if p <= -10_000:
            overflow += 1
        else:
            idx = min(int((p + 10_000) // 500), len(bins) - 1)
            bins[idx]["n"] += 1
    return [{"overflow": True, "n": overflow}] + bins


def read_summary(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M1-A 回测可视化报告（只读 CSV，自包含 HTML）")
    ap.add_argument("--indir", default="experiments/m1a", help="回测输出目录")
    ap.add_argument("--out", default=None, help="输出 HTML 路径（默认 <indir>/report.html）")
    args = ap.parse_args(argv)

    indir = Path(args.indir)
    out = Path(args.out) if args.out else indir / "report.html"
    states_path, trades_path, prices_path = (
        indir / "states.csv",
        indir / "trades.csv",
        indir / "prices.csv",
    )
    missing = [p.name for p in (states_path, trades_path, prices_path) if not p.exists()]
    if missing:
        print(f"[错误] 缺少输入文件: {', '.join(missing)}（目录 {indir}）")
        return 2

    dates, eq, bench, pos = load_states(states_path)
    trades = load_trades(trades_path)
    pdates, pcloses = load_prices(prices_path, dates[0], dates[-1])
    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n
    total_pnl = sum(pnls)
    median_pnl = sorted(pnls)[n // 2]
    worst = min(trades, key=lambda t: t["pnl"])
    best = max(trades, key=lambda t: t["pnl"])
    avg_hold = sum(t["hold"] for t in trades) / n
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
    n_tp = by_reason.get("take_profit", 0)
    n_dte = by_reason.get("dte_exit", 0)
    tp_pnls = [t["pnl"] for t in trades if t["exit_reason"] == "take_profit"]
    dte_pnls = [t["pnl"] for t in trades if t["exit_reason"] == "dte_exit"]

    dd = drawdowns(eq)
    dd_bench = drawdowns(bench)
    dd_min = min(dd)
    dd_min_date = dates[dd.index(dd_min)]
    bench_dd_min = min(dd_bench)
    bench_dd_min_date = dates[dd_bench.index(bench_dd_min)]
    total_return = eq[-1] / START_CASH - 1.0
    bench_return = bench[-1] / bench[0] - 1.0
    bh_final = bench[-1] / bench[0] * START_CASH

    def in_window(d: str, frm: str, to: str) -> bool:
        return frm <= d <= to

    phase_annotations: list[dict] = []
    phase_stats: list[dict] = []
    for name, frm, to in PHASES:
        seg_dates = [d for d in dates if in_window(d, frm, to)]
        seg_dd = [v for d, v in zip(dates, dd, strict=True) if in_window(d, frm, to)]
        if seg_dd:
            local = min(seg_dd)
            ld = seg_dates[seg_dd.index(local)]
            if local <= -0.15 and ld != dd_min_date:
                phase_annotations.append(
                    {"t": ld, "v": local, "label": f"{name} {fmt_pct(local)}（{ld}）"}
                )
        ex = [t for t in trades if in_window(t["exit_date"], frm, to)]
        phase_stats.append(
            {
                "name": name,
                "tp": sum(1 for t in ex if t["exit_reason"] == "take_profit"),
                "dte": sum(1 for t in ex if t["exit_reason"] == "dte_exit"),
                "big": sum(1 for t in ex if t["pnl"] < -5000),
            }
        )
    annotations = [
        {
            "t": dd_min_date,
            "v": dd_min,
            "label": f"最大回撤 {fmt_pct(dd_min)}（{dd_min_date}）",
        }
    ] + phase_annotations

    bins = histogram(pnls)
    n_over = bins[0]["n"]

    # ---- 与 summary.txt 交叉核对（只报告，不纠正）----
    summary = read_summary(indir / "summary.txt")
    checks = {
        "final_equity": eq[-1],
        "trades": n,
        "sessions": len(dates),
        "max_drawdown": dd_min,
    }
    for key, actual in checks.items():
        want = summary.get(key)
        if want is not None and abs(float(want) - float(actual)) > 0.5:
            print(f"[不一致] {key}: summary.txt={want} 报告={actual}")

    # ---- HTML 内容 ----
    title = "Sell Put 策略 · M1-A 回测报告（2005–2024）"
    sub = (
        f"标的 SPY · 真实日线 + 合成期权报价（研究近似）· {len(dates)} 个交易日 · "
        "由 experiments/m1a 的 CSV 重新计算绘制，未改动任何回测逻辑"
    )
    guide = (
        "<h2>3 分钟怎么读这份报告</h2><ol>"
        f"<li>先看第 1 张图定结论：策略 20 年 {fmt_pct(total_return)}，买并持有 "
        f"{fmt_pct(bench_return)}——策略大幅跑输基准。</li>"
        f"<li>再看第 2 张图看风险：最大回撤 {dd_min:.1%}（{dd_min_date}）——"
        "无止损裸卖 Put 有清盘级风险。</li>"
        "<li>最后看第 4、5 张图找原因：收益靠大量小额止盈堆出来，亏损集中在 "
        "2008/2020 少数巨亏笔。</li>"
        "<li>灰底阴影 = 三段历史熊市背景（2008 金融危机 / 2020 COVID / 2022 加息）；"
        "鼠标悬停任意曲线看每日数值，悬停时间线上的线看单笔明细。</li>"
        "</ol>"
    )
    cards = [
        ("期末净值", fmt_money(eq[-1]), f"20 年总收益 {fmt_pct(total_return)}"),
        ("买并持有 SPY", fmt_money(bh_final), f"同期 {fmt_pct(bench_return)}"),
        ("最大回撤", f"{dd_min:.1%}", dd_min_date),
        ("交易", f"{n} 笔 · 胜率 {win_rate:.1%}", f"平均持有 {avg_hold:.0f} 天"),
        ("离场构成", f"止盈 {n_tp} · 强退 {n_dte}", "无止损 · 无滚仓"),
        ("单笔最差", fmt_money(worst["pnl"]), worst["exit_date"]),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="d">{d}</div></div>'
        for k, v, d in cards
    )

    note1 = (
        "蓝线 = 策略账户总资产（起始 $100,000），橙线 = 同样 $100,000 直接买 SPY 持有不动"
        "（价格比，不含分红再投资）。灰底阴影 = 三段历史熊市背景。"
    )
    note2 = "回撤 = 相对历史最高点的下跌幅度，0% 是顶部；红点标出各阶段最深坑位。"
    note3 = "SPY 每股收盘价；阴影只是背景标注，不是策略信号。"
    note4 = (
        "每笔交易的最终盈亏（已实现，含双边佣金）。每根柱宽 $500，"
        "最左灰柱把 ≤ −$10,000 的大亏合并在一起。"
    )
    note5 = (
        "每笔交易一条横线：左端深色圆点 = 开仓日，线长 = 持有期，颜色 = 离场方式；"
        "按开仓日期从上到下排列。悬停看单笔明细。"
    )

    ex1 = explain(
        [
            "这张图是什么：假设 2005 年初投入 $100,000——蓝线是按策略滚动卖 Put 的"
            "账户总资产，橙线是同一笔钱直接买 SPY 持有不动。",
            "该观察什么：两条线谁高谁低；蓝线在三段灰底熊市里跌得有多狠、"
            "之后用几年才爬回前高。",
            f"说明了什么：策略 20 年总收益 {fmt_pct(total_return)}，买并持有 "
            f"{fmt_pct(bench_return)}——策略只赚到基准的约 {total_return / bench_return:.0%}。"
            "卖 Put 赚的是平静期的保费，却在两次危机里把多年利润一次性赔掉，"
            "收益与风险完全不对等。",
        ]
    )
    ex2 = explain(
        [
            "这张图是什么：账户从历史最高点回落的幅度，跌得越深离\u2018清零\u2019越近；"
            "红线=策略，灰线=SPY 自身回撤作对照。",
            "该观察什么：红点标出的坑底时间与深度；两个大坑之间隔了几年、爬回去用了多久；"
            "红线和灰线的差距（策略是否比市场跌得更深）。",
            f"说明了什么：2008 年最深 {dd_min:.1%}（{dd_min_date}），2020 年又跌过半；"
            f"SPY 自身最深也才 {bench_dd_min:.1%}（{bench_dd_min_date}），策略回撤是它的 "
            f"{dd_min / bench_dd_min:.1f} 倍——裸卖 Put 不仅不避险，反而放大了下行，"
            "而且规则里没有任何止损保护。",
        ]
    )
    ex3 = explain(
        [
            "这张图是什么：SPY 每股收盘价的 20 年走势，灰底阴影标出三段著名熊市"
            "（只是背景标注）。",
            "该观察什么：长期方向与三段急跌的位置、幅度；对照图 1、图 2 看"
            "每次急跌时策略净值跌了多少。",
            f"说明了什么：标的本身是长期牛市资产（{fmt_pct(bench_return)}），"
            "策略几乎全部的亏损都来自这三段急跌——卖 Put 的命门是"
            "\u2018标的大幅快速下跌\u2019，而不是长期趋势。",
        ]
    )
    final_pos_note = ""
    if abs(pos[-1]) > 0.5:
        final_pos_note = (
            f"期末仍持有未平仓合约（市值 {fmt_money(pos[-1])}），"
            "所以净值 ≠ 起始资金 + 已实现 PnL 合计。"
        )
    ex4 = explain(
        [
            f"这张图是什么：{n} 笔交易每笔最终盈亏的直方图；绿柱=盈利，红柱=亏损，"
            f"最左灰柱把 ≤ −$10,000 的大亏合并（共 {n_over} 笔）。",
            f"该观察什么：柱子是不是\u2018大量小绿柱 + 少量深红柱\u2019；"
            f"均值 {fmt_money(total_pnl / n)} 与中位数 {fmt_money(median_pnl)} 差多少。",
            f"说明了什么：胜率 {win_rate:.1%}（{len(wins)} 笔盈利，平均 "
            f"{fmt_money(sum(wins) / len(wins))}），但 {len(losses)} 笔亏损平均 "
            f"{fmt_money(sum(losses) / len(losses))}——最惨一笔（{worst['exit_date']}）亏 "
            f"{fmt_money(worst['pnl'])}，最好一笔（{best['exit_date']}）赚 "
            f"{fmt_money(best['pnl'])}，单笔盈亏严重不对称。20 年已实现 PnL 合计 "
            f"{fmt_money(total_pnl)}。{final_pos_note}",
        ]
    )
    phase_bits = [
        f"{ps['name']} 阶段 {ps['dte']} 笔强退、{ps['big']} 笔亏损超 $5,000"
        for ps in phase_stats
    ]
    ex5 = explain(
        [
            "这张图是什么：每笔交易一条横线——左端深色圆点=开仓日，线长=持有期，"
            "颜色=离场方式（绿=止盈 50%，橙=DTE≤3 强退）；按开仓日期从上到下排列。",
            "该观察什么：三段灰底熊市里线条是否变密、橙色是否成串出现；"
            "平静年份是否以短绿线为主。",
            f"说明了什么：{n_tp} 笔止盈平均 {fmt_money(sum(tp_pnls) / len(tp_pnls))}，"
            f"靠小赢积累；{n_dte} 笔强退平均 {fmt_money(sum(dte_pnls) / len(dte_pnls))}，"
            f"集中在危机期——{'；'.join(phase_bits)}。危机来临时止盈规则等不到、"
            "被迫强退接盘，这正是 M2 止损/滚仓要解决的问题。",
        ]
    )

    footer = (
        "<h3>数据与口径</h3><ul>"
        f"<li>数据来源：experiments/m1a/ 下 summary.txt / states.csv / trades.csv / "
        f"prices.csv（seed=42，{dates[0]} ~ {dates[-1]}，{len(dates)} 个交易日）。"
        "本报告只读取这些 CSV 重新计算绘制，未重跑回测引擎、未改动任何回测逻辑。</li>"
        "<li>期权报价为合成报价（Black-Scholes；波动率 = SPY 20 日滚动已实现波动率，"
        "股息率 = 近 12 个月真实分红）——不是真实历史期权链，所有数字都是研究近似；"
        "真实期权链在 M2 接入。</li>"
        "<li>买并持有基准 = 期末收盘价 ÷ 期初收盘价，不含分红再投资（实际会略高）。</li>"
        "<li>账户假设：起始 $100,000；简化 Reg-T 保证金；每笔 PnL 含双边佣金；"
        "仓位上限 50%。</li>"
        "<li>策略规则：滚动卖约 30 DTE、Δ≈0.20 的虚值 Put；浮盈达 50% 止盈；"
        "DTE ≤ 3 强退；无止损、无滚仓（M2 再议）。</li>"
        "<li>重新生成：uv run python scripts/build_report.py（在 options-platform 目录下）。"
        "</li><li>打开方式：双击 report.html 用浏览器打开即可，无需联网。</li></ul>"
    )

    template = (
        Path(__file__).with_name("report_template.html").read_text(encoding="utf-8")
    )
    html = (
        template.replace("__TITLE__", title)
        .replace("__SUB__", sub)
        .replace("__GUIDE__", guide)
        .replace("__CARDS__", cards_html)
        .replace("__NTRADES__", str(n))
        .replace("__NOTE1__", note1)
        .replace("__NOTE2__", note2)
        .replace("__NOTE3__", note3)
        .replace("__NOTE4__", note4)
        .replace("__NOTE5__", note5)
        .replace("__EX1__", ex1)
        .replace("__EX2__", ex2)
        .replace("__EX3__", ex3)
        .replace("__EX4__", ex4)
        .replace("__EX5__", ex5)
        .replace("__FOOTER__", footer)
        .replace(
            "__DATA__",
            json.dumps(
                {
                    "meta": {
                        "start": dates[0],
                        "end": dates[-1],
                        "start_cash": START_CASH,
                        "sessions": len(dates),
                        "n_trades": n,
                    },
                    "phases": [{"name": n_, "from": f, "to": t} for n_, f, t in PHASES],
                    "states": {"t": dates, "eq": eq, "bench": bench},
                    "prices": {"t": pdates, "c": pcloses},
                    "trades": trades,
                    "reasons": [
                        {"key": k, "label": v}
                        for k, v in REASON_ZH.items()
                    ],
                    "dd_annotations": annotations,
                    "bins": bins,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"已生成: {out}（{out.stat().st_size / 1024:.0f} KB）")
    print(
        f"  期末净值 {fmt_money(eq[-1])}（{fmt_pct(total_return)}）vs "
        f"买并持有 {fmt_pct(bench_return)}"
    )
    print(f"  最大回撤 {dd_min:.1%} @ {dd_min_date}；交易 {n} 笔，胜率 {win_rate:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
