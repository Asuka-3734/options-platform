"""M0 演示：一次 Sell Put 回测 → 人类可读报告 + CSV 导出。

用法（在 options-platform 目录下）：
    uv run python scripts/demo_m0.py

产出：
    - 终端里打印一份中文报告（概览 / 交易明细 / 每日状态样例 / 字段说明）
    - experiments/demo_m0/states.csv   每日账户状态（一条一天）
    - experiments/demo_m0/trades.csv   每笔开平配对交易（分析的最小单元）

注意：M0 数据层是合成数据（GBM + BS 报价），数字只用于演示与验证
引擎语义，不代表 SPY 真实历史。真实行情是 M1（yfinance）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

from sellput.config import BacktestConfig, DataConfig, SyntheticConfig
from sellput.instruments import NyseCalendar
from sellput.market_data import synthetic_provider_from_config
from sellput.sim import SimulationEngine

# Windows 控制台默认 GBK，强制 UTF-8 输出中文报告
sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 1. 配置：想改策略 / 成本 / 起始资金，改这里（字段含义见技术规格 §4.4）
# ---------------------------------------------------------------------------
CONFIG = BacktestConfig(
    data=DataConfig(
        provider="synthetic",
        symbol="SPY",  # 换成 "SPX" 即自动切换：欧式 + 现金结算 + BS 定价
        start=date(2021, 1, 1),
        end=date(2024, 12, 31),
        synthetic=SyntheticConfig(
            seed=42,
            s0=380.0,        # 起始标的价（合成 GBM 起点）
            sigma=0.20,      # 标的波动率
            drift=0.08,      # 年化漂移（长期预期收益）
            iv_atm=0.20,     # ATM 隐含波动率
            skew=-0.3,       # 负偏斜：越低的行权价 IV 越高（贴近现实）
            q=0.015,         # 连续股息率
        ),
    ),
    # 默认值：fill=next_open_mid、滑点 5bps、0.65/张 + 1/单、简化 Reg-T、
    # 50% 资金分配、Sell Put 四规则（DTE30 / Δ0.20 / 止盈 50% / DTE≤3 强退）
)

OUT_DIR = Path("experiments/demo_m0")


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def main() -> None:
    engine = SimulationEngine(CONFIG)
    provider = synthetic_provider_from_config(CONFIG.data, CONFIG.market.rate)
    result = engine.run(provider, NyseCalendar())

    states = result.states
    trades = result.trades
    start_cash = CONFIG.account.starting_cash
    final_equity = states[-1].equity
    total_return = final_equity / start_cash - 1.0

    # 基准：期初买并持有到期末的收益率（仅合成路径演示用）
    bench = states[-1].benchmark_price / states[0].benchmark_price - 1.0

    # 最大回撤（equity 序列）
    eq = pd.Series([s.equity for s in states])
    drawdown = (eq / eq.cummax() - 1.0).min()

    # ------------------------------------------------------------------
    # 2. 概览
    # ------------------------------------------------------------------
    print("=" * 78)
    print("Sell Put 回测报告（M0 · 合成数据演示）")
    print("=" * 78)
    print(f"标的: {CONFIG.data.symbol}   区间: {CONFIG.data.start} → {CONFIG.data.end}")
    print(f"交易日: {len(states)}   引擎耗时: {result.duration_seconds:.2f}s")
    print("-" * 78)
    print(f"起始资金:            {start_cash:>14,.2f}")
    print(f"期末净值 (equity):   {final_equity:>14,.2f}")
    print(f"策略总收益:          {pct(total_return):>14}")
    print(f"买并持有基准:        {pct(bench):>14}")
    print(f"超额收益:            {pct(total_return - bench):>14}")
    print(f"最大回撤:            {pct(drawdown):>14}")

    if trades:
        wins = [t for t in trades if t.pnl > 0]
        total_pnl = sum(t.pnl for t in trades)
        hold = [(t.exit_date - t.entry_date).days for t in trades]
        by_reason = pd.Series([t.exit_reason for t in trades]).value_counts()
        print(f"交易笔数:            {len(trades):>14}   (胜率 {len(wins) / len(trades):.1%})")
        print(f"已实现 PnL 合计:     {total_pnl:>14,.2f}")
        print(f"平均每笔:            {total_pnl / len(trades):>14,.2f}")
        print(f"平均持有天数:        {sum(hold) / len(hold):>14.1f}")
        print("离场原因分布: " + ", ".join(f"{k}={v}" for k, v in by_reason.items()))
    else:
        print("（无成交）")
    print("=" * 78)

    # ------------------------------------------------------------------
    # 3. 交易明细（前 10 笔；全量在 CSV）
    # ------------------------------------------------------------------
    if trades:
        print("\n交易明细（前 10 笔）:")
        rows = [
            {
                "id": t.id,
                "开仓日": t.entry_date,
                "平仓日": t.exit_date,
                "到期": t.spec.expiry,
                "行权价": t.spec.strike,
                "张数": t.qty,
                "开仓价": round(t.entry_price, 2),
                "平仓价": round(t.exit_price, 2),
                "PnL": round(t.pnl, 2),
                "离场": t.exit_reason,
                "入场DTE": t.dte_at_entry,
                "入场Δ": round(t.delta_at_entry, 3),
                "入场IV": round(t.entry_iv, 4),
            }
            for t in trades[:10]
        ]
        print(pd.DataFrame(rows).to_string(index=False))
        print("\n字段说明:")
        print("  张数  <0 = 空头卖出（本策略永远为负）")
        print("  PnL   = (平仓价-开仓价)×张数×100 - 双边手续费（已全部计入）")
        print("  离场  entry=新开 / take_profit=止盈50% / dte_exit=DTE≤3强退")
        print("        expire=到期作废 / assign=到期指派(次日开盘卖股) / assign_cash=现金结算")

    # ------------------------------------------------------------------
    # 4. 每日状态（最后 5 天；全量在 CSV）
    # ------------------------------------------------------------------
    print("\n每日状态（最后 5 天）:")
    srows = [
        {
            "日期": s.date,
            "equity": round(s.equity, 2),
            "现金": round(s.cash, 2),
            "持仓市值": round(s.positions_value, 2),
            "保证金占用": round(s.margin_used, 2),
            "组合Δ": round(s.greeks["delta"], 2),
            "组合Θ": round(s.greeks["theta"], 2),
            "当日归因PnL": round(s.attribution["total"], 2),
            "标的收盘": round(s.benchmark_price, 2),
        }
        for s in states[-5:]
    ]
    print(pd.DataFrame(srows).to_string(index=False))
    print("\n字段说明:")
    print("  equity   = 现金 + 持仓市值（负债方向：空头 Put 持仓市值为负）")
    print("  组合Δ    空头 Put -> 正值：标的涨赚钱、跌亏钱")
    print("  组合Θ    正值 = 每天靠时间流逝收钱（Sell Put 的核心收入）")
    print("  当日归因  equity 变化 ≈ Δ·dS + ½Γ·dS² + Θ·dt + Vega·dIV + 残差（近似分解）")

    # ------------------------------------------------------------------
    # 5. CSV 导出
    # ------------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "date": s.date,
                "equity": s.equity,
                "cash": s.cash,
                "positions_value": s.positions_value,
                "margin_used": s.margin_used,
                "benchmark_close": s.benchmark_price,
                "g_delta": s.greeks["delta"],
                "g_gamma": s.greeks["gamma"],
                "g_theta": s.greeks["theta"],
                "g_vega": s.greeks["vega"],
                "a_total": s.attribution["total"],
                "a_delta": s.attribution["delta"],
                "a_theta": s.attribution["theta"],
                "a_vega": s.attribution["vega"],
                "a_residual": s.attribution["residual"],
                "n_positions": len(s.positions),
            }
            for s in states
        ]
    ).to_csv(OUT_DIR / "states.csv", index=False)
    pd.DataFrame(
        [
            {
                "id": t.id,
                "strike": t.spec.strike,
                "expiry": t.spec.expiry,
                "qty": t.qty,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "commissions": t.commissions,
                "exit_reason": t.exit_reason,
                "entry_iv": t.entry_iv,
                "exit_iv": t.exit_iv,
                "dte_at_entry": t.dte_at_entry,
                "delta_at_entry": t.delta_at_entry,
            }
            for t in trades
        ]
    ).to_csv(OUT_DIR / "trades.csv", index=False)
    print(f"\n已导出: {OUT_DIR / 'states.csv'} 和 {OUT_DIR / 'trades.csv'}")
    print("想改参数？编辑本文件 CONFIG 段后重跑即可；结果完全可复现（固定 seed）。")


if __name__ == "__main__":
    main()
