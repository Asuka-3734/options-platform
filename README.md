# sellput — Options Research Platform

个人期权策略研究与回测平台（Phase 1：Sell Put；M1-B 起为多策略引擎）。规格与决策
记录见 [docs/technical-spec.md](docs/technical-spec.md)（§12 为 M0 验收标准，§13 为
M1-A 验收标准，§14 为 M1-B 多策略验收标准）。

## 目录结构

```
src/sellput/     # 核心库（M0 垂直切片 + M1-A 真实数据接入）
tests/           # 对拍 / 防前视 / 生命周期 / 四规则 / 复现 / 重算 / M1-A 数据层测试
scripts/         # bench_m0.py：性能 benchmark；run_backtest.py：一键回测（M1-B 多策略）；demo_m0.py：合成数据演示；build_report.py：可视化 HTML 报告
configs/         # 示例配置（M1-B 起使用）
notebooks/       # 研究示例（M1-B 起使用）
data/ runs/      # 本地缓存与实验输出（gitignore）
```

## 快速开始

```bash
uv sync                      # 安装依赖（Python >= 3.12）
uv run pytest                # 全部功能测试（benchmark 默认排除）
uv run pytest -m benchmark   # 显式运行 benchmark
uv run python scripts/bench_m0.py   # 10 年合成回测耗时（目标 <10s）
```

## M1-A：真实 SPY 日线回测（一键）

```bash
# 默认 2005-01-01 ~ 2024-12-31
uv run python scripts/run_backtest.py

# 自定义区间 / 离线模式（只用本地缓存或手动 CSV，不联网）
uv run python scripts/run_backtest.py --start 2015-01-01 --end 2024-12-31
uv run python scripts/run_backtest.py --offline
```

数据获取三级顺序：本地缓存 `data/spy_daily.csv` → yfinance（成功后自动写缓存）
→ 手动 CSV 兜底（`data/` 下任意 CSV，列 `Date,Open,High,Low,Close,Volume[,Dividends]`）。

**网络说明**：Windows 上 Python 默认不走系统代理；数据中心出口 IP 直连 Yahoo 会被
429 限流。若本机有本地代理软件在跑，先配置环境变量（主机与端口以你的代理软件为准，
下为占位示例）：

```powershell
setx HTTPS_PROXY "http://127.0.0.1:<port>"   # <port> 换成你代理软件的实际端口
setx HTTP_PROXY "http://127.0.0.1:<port>"    # 重开终端后生效
```

产出（`experiments/m1a/`）：`equity_curve.png`（策略净值 vs 买并持有 SPY + 回撤副图）、
`states.csv`（每日账户状态）、`trades.csv`（每笔交易）、`prices.csv`（实际价格序列）、
`summary.txt`。

## M1-B：多策略架构（Sell Put / Buy & Hold）

```bash
# Sell Put（默认，M0 四规则）
uv run python scripts/run_backtest.py --offline

# Buy & Hold：首日全仓买入 SPY，期末最后交易日开盘清仓（分红不付现，与价格型基准口径一致）
uv run python scripts/run_backtest.py --strategy buy_hold --offline --out experiments/m1b
```

- 多策略内核：订单（`OrderIntent.asset` 支持期权/股票）、组合（期权 FIFO + 股票 lot）、
  引擎（期末 `on_final` 钩子）、Trade（`asset_kind`）均已泛化；Sell Put 行为与 M1-A 逐位一致。
- trades.csv 增加 `asset_kind` / `symbol` 列（期权行数值不变）。
- 可视化报告兼容两种策略：`uv run python scripts/build_report.py --indir experiments/m1b`
  （生成的 report.html 双击打开，无需联网）。
- 明确不做（M1-B）：其他策略（CC/Wheel/Spread）、Sell Put 参数配置化、analysis 指标与
  sweep（推迟到 M1-C）。

## M0 / M1-A 范围

- 定价：Black-Scholes（欧式解析 + 解析 Greeks）与 CRR 二叉树（美式，标准实现、无高级优化）；
  BS 用于美式标的时为近似，`PriceResult.model` 标注 `black_scholes_approx`（Spec D6）。
- 保证金：Simplified Reg-T-style（research approximation，非券商 Reg-T 复现）与 Cash-Secured。
- 引擎：日频事件循环，收盘信号（Close(t−1)）→ 次日开盘成交（Open(t)），严格防前视。
- 策略：Sell Put 四默认规则（DTE 30 / delta 0.20 / 止盈 50% / DTE≤3 强制退出）；SL/Roll 属 M2。
- M1-A 数据：标的 = 真实 SPY 日线（未复权）+ 真实分红折算股息率；期权报价仍为合成
  （BS 定价，波动率 = SPY 20 日滚动已实现波动率，开盘快照用 t−1 值）。真实历史期权
  链属 M2。
