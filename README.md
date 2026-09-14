# sellput — Options Research Platform

个人期权策略研究实验室（Phase 1：Sell Put；M1-B 起为多策略引擎；**M1-C 起具备参数研究与
样本外纪律**）。规格与决策记录见 [docs/technical-spec.md](docs/technical-spec.md)：
§12/§13/§14 为 M0 / M1-A / M1-B 验收标准，**§15 为 M1-C（基础研究能力）验收标准**。

> **定位**：这是一套"研究工具"，不是交易系统，也不是真实历史期权行情的回测器——
> 当前期权报价是**合成**的，使用前请读下面的「研究效度与使用边界」。

## 目录结构

```
src/sellput/     # 核心库
  analysis.py    #   指标（Spec §6/§15.5；纯函数，M1-C）
  vol.py         #   波动率统计与 RV 四桶（M1-C）
  research.py    #   研究层：单次运行 / train-test 切分 / sweep / manifest（M1-C）
  sim.py 等      #   引擎与领域模块（M0/M1-A/M1-B；M1-C 未改动）
tests/           # 213 项功能测试（对拍/防前视/生命周期/四规则/复现/重算/指标/sweep/报告）
scripts/         # run_backtest.py 一键回测 ｜ run_sweep.py 参数 sweep ｜
                 # finalize_experiment.py 最终样本外评估 ｜ build_sweep_report.py 研究报告 ｜
                 # compare_runs.py 实验对比 ｜ bench_m0.py 性能 benchmark ｜
                 # build_report.py M1-A 可视化 HTML ｜ demo_m0.py 合成数据演示
docs/            # technical-spec.md（唯一需求基准）
data/            # 本地价格缓存（gitignore）
runs/            # 实验输出（gitignore）：sweep 目录 / 最终评估 / 报告
experiments/     # M1-A / M1-B 的历史产物（gitignore）
```

## 快速开始

```bash
uv sync                             # 安装依赖（Python >= 3.12）
uv run pytest                       # 全部功能测试（benchmark 默认排除）
uv run pytest -m benchmark          # 性能 benchmark

# 一键回测
uv run python scripts/run_backtest.py --offline                        # Sell Put（默认参数）
uv run python scripts/run_backtest.py --strategy buy_hold --offline --out experiments/m1b
```

## M1-C：参数研究（sweep / Train-Test / 约束筛选 / 研究报告）

### 1) 策略参数可配置（Spec §15 F1）

```bash
uv run python scripts/run_backtest.py --offline --dte 45 --delta 0.15 --tp 0.25
```

`--dte --delta --tp`，另有 `--dte-exit --entry-frequency --max-open-positions`；
默认值 30 / 0.20 / 0.50 / 3 / weekly / 1（Spec §5），越界取值（如 δ ≥ 0.5）会被 Pydantic 拒绝。

### 2) 参数 sweep（**只看 train**）

```bash
uv run python scripts/run_sweep.py --dte 20,30,45,60 --delta 0.10,0.15,0.20,0.25,0.30 \
  --tp 0.25,0.50,0.75,1.00 --start 2005-01-01 --end 2024-12-31 --train-end 2018-12-31 \
  --offline --workers 8 --out runs/sweep-official
```

- **搜索只读 train 段**：每个组合都**不计算** test 指标（manifest 记 `test_eval_count = 0`），
  指标 CSV 与组合清单里也不含 `full_*` / `test_*` 列——样本外数字在搜索阶段根本不产生。
- **metrics-first**（Spec §15 N4）：默认只写每组合 `manifest.json` + `metrics.json` 与 sweep 级
  `sweep_metrics.csv` / `sweep_manifest.json`；`--save-all` 才追加 states / trades / prices。
- **MDD 约束筛选**：`--max-mdd 0.30 [--sort-by cagr]`（阈值填小数）。
  若没有任何组合满足约束，系统输出**「无可行解」**并附最小回撤组合，且明确标注
  **「最接近约束 ≠ 满足约束」**——不会自动放宽阈值，也不会用"最接近"冒充可行解。
- **可复现**（Spec §15 N2/AC-7）：`sweep_metrics.csv` 只含确定性字段，同数据 + 同配置连跑两遍
  **逐字节一致**，且结果与进程数、网格顺序无关。

### 3) Train / Test 切分与最终样本外评估

```bash
uv run python scripts/finalize_experiment.py --dte 30 --delta 0.30 --tp 0.50 \
  --start 2005-01-01 --end 2024-12-31 --train-end 2018-12-31 --offline \
  --out runs/sweep-official/final
```

固定切分 **train 2005–2018 / test 2019–2024**（Spec §15 F5）。test 段只用于这一次最终评估
（manifest 记 `test_eval_count = 1`）；产出 train / test / full 三段指标 + **双基准**
（价格型与总回报型）+ 全量制品，并打印成表：

| 段 | 总收益 | CAGR | 最大回撤 | 笔数 | 胜率 | 基准(价格) | 基准(总回报) |
|---|---|---|---|---|---|---|---|
| train | … | … | … | … | … | … | … |
| test | … | … | … | … | … | … | … |
| full | … | … | … | … | … | … | … |

口径：交易按**平仓日**归属分段（保证 train 段指标不依赖 test 数据）；test 段以 train 末净值
为起点，因此 `(1+train)(1+test) = 1+full`；总回报基准按"除息日分红 ÷ 次一交易日开盘价"再投。

### 4) 研究报告与实验对比

```bash
uv run python scripts/build_sweep_report.py --sweep runs/sweep-official \
  --final runs/sweep-official/final --max-mdd 0.30 \
  --out-html runs/sweep-official/report.html --out-md runs/sweep-official/research_report.md

uv run python scripts/compare_runs.py --runs runs/sweep-official/final runs/other-exp/final
```

- 报告含：运行信息与**复现命令**、DTE×Delta 热图（6 个指标，各带 1D 边际）、敏感性观察、
  约束筛选（含无可行解）、Top-N、最终样本外评估 + **实际入场 DTE 分桶** + **RV 四桶** +
  双基准、金融假设登记表、研究结论、效度声明。HTML **离线自包含**（双击可开，无需联网）。
- `compare_runs.py` 支持 `--segment train|test|full` 与 `--sort-by`；其中 **test 段禁止排序**
  （Spec §15 F8 / D14：不得按样本外结果选参数）。

## 研究效度与使用边界（重要）

1. **期权报价是合成的**：BS 定价，`iv_atm` = SPY 20 日滚动**已实现波动率**（t−1 口径），
   skew = 0、价差 5bps 常数。真实期权价格含**波动率风险溢价**，因此本平台的收益/风险数字是
   **研究近似**，不得当作真实历史期权表现。
2. **合成链只有月度到期**：目标 DTE 与实际入场 DTE 会相差约 ±10 个交易日；参数敏感性必须按
   **实际入场 DTE** 分桶解释（报告已如此呈现，`Trade.dte_at_entry`）。
3. **保证金政策 `reject` 不模拟追保强平**：亏损可以超过 100% 权益（个别参数组合会"打穿账户"），
   这是模型产物而非真实爆仓路径。
4. **无 Stop Loss / Roll**（属 M1 范围之外；M2 第一批研究问题，不预设结论）。
5. **参数搜索结果不是预测**：报告只表述为"在当前模型、数据与研究区间下，训练集内表现最好"；
   test 仅用于样本外验证，且样本有限、含统计噪声。
6. 完整假设清单见 Spec §17.1，报告效度声明模板见 Spec §17.3。

## M0 / M1-A / M1-B（历史交付摘要）

- **定价**：Black-Scholes（欧式解析 + 解析 Greeks）与 CRR 二叉树（美式）；BS 用于美式标的时
  标注 `black_scholes_approx`（Spec D6）。
- **保证金**：Simplified Reg-T-style（research approximation，非券商 Reg-T 复现）与 Cash-Secured。
- **引擎**：日频事件循环，收盘信号（Close(t−1)）→ 次日开盘成交（Open(t)），严格防前视。
- **策略**：Sell Put 四规则（DTE 30 / δ0.20 / TP 50% / DTE≤3 强退）+ Buy & Hold（M1-B）。
- **数据**：真实 SPY 未复权日线（本地缓存 → yfinance → 手动 CSV 三级）+ 真实分红折算股息率；
  **真实历史期权链属 M3**（M1-C / M2 不做）。
- **网络说明**：Windows 上 Python 默认不走系统代理；若直连被限流，可配置
  `HTTPS_PROXY` / `HTTP_PROXY`（占位示例 `http://127.0.0.1:<port>`，端口换成你的代理）或使用
  `--csv` 手动数据兜底。

## 验收状态

| 里程碑 | 验收 | 证据 |
|---|---|---|
| M0 | §12 九项 | 定价对拍 / 不变量 / 金标准 / 防前视 / 确定性测试 |
| M1-A | §13 八项 | 2005–2024 真实 SPY 首跑（173,061.18 / −85.05% / 529 笔） |
| M1-B | §14 八项 | 多策略架构 + Buy & Hold（484,689.27）+ Sell Put 逐位回归 |
| **M1-C** | **§15 十五项** | 213 项测试；80 组合 sweep（366s）；无可行解行为；train/test 隔离结构性测试；双基准；报告 |
