# Options Research Platform — Technical Specification

> **状态**：v0.7.0 · **M0 已完成**（§12 九项验收全部通过）· **M1-A 已完成**（§13 八项验收全部通过，2005–2024 真实 SPY 首跑出曲线）· **M1-B 已完成**（§14 八项验收全过）· **M1-C 已完成**（§15 十五项验收全部通过；213 项测试全绿；12 项决策见 §10/§11）
> **项目代号**：`sellput`（阶段一策略为 Sell Put；平台本体面向全部期权策略）
> **定位**：本地运行的个人期权策略研究与回测平台 —— 策略定义 → 参数配置 → 历史回测 / Monte Carlo → 分析可视化 → 实验对比。
> **语言约定**：代码、配置、日志为英文；文档为中文。

**v0.3 变更记录**（按用户 8 条意见）：D17 分红模型抽象化 · §5 策略默认值简化为 M0 四规则（SL/Roll 移 M2）· D9 成交假设增加"信号/执行快照分离、禁未来数据"· D14 明确 train/test 使用纪律 · §6 增加 IV Rank/IVP/RV 与分桶统计并封版 · §4.0 新增 Raw/Derived 数据边界 · D6 明确 PricingEngine 抽象（SPX→BS，SPY→CRR）· §4.1 核心对象模型细化（MarketSnapshot/Order/Fill/Position/PortfolioState 生命周期）。

**v0.4 变更记录**（决策全部确认，M0 已获授权）：D6 CRR 进入 M0（标准实现、无高级 early-exercise 优化、报告标注模型）· D11 更名 Simplified Reg-T-style Margin Model（research approximation 声明）· D12 SPX 结算明确为 M0 简化模型 · 性能数值定性为 benchmark target（非 correctness gate）· §8 里程碑重构（M0 扩展为垂直切片 + 正确性闸门）· 新增 §12 M0 Acceptance Criteria · §4.1 落实最小可用模型原则。

**v0.5 变更记录**（M1-A 已获授权并完成）：M1 拆分为 M1-A（真实标的日线接入 + 首条收益曲线）/ M1-B（指标、CLI、sweep）/ M1-C（报告增强）· 新增 HybridProvider（真实价格 + 合成链，20 日滚动已实现波动率驱动）· 数据三级获取（缓存 → yfinance → 手动 CSV 兜底）· 按日股息率（真实分红折算，PerDayDividendModel）· 新增 §13 M1-A Acceptance Criteria。

**v0.6 变更记录**（M1-B 已获授权，进行中）：M1-B 重定义为**多策略架构**（Sell Put + Buy & Hold，通用回测引擎）——原 M1-B 内容（analysis 指标、CLI 参数化、sweep + train/test 纪律）推迟为 M1-C；报告增强已提前交付（experiments/m1a/report.html）· 订单模型泛化（`OrderIntent.asset: OptionSpec | EquitySpec`）· Trade 泛化（`asset_kind` + 期权专属字段可空）· `BuyHoldStrategy`（全仓买入持有、期末开盘清仓、分红不付现）· `Strategy.on_final` 期末钩子 · CLI `--strategy` · 新增 §14 M1-B Acceptance Criteria。

**v0.7 变更记录**（M1-C 已立项，12 项决策全部确认，§10/§11）：**里程碑重排**——M1-C=基础研究能力（参数配置化 / 网格 sweep / 固定 train-test / RV 市场环境分桶 / 基础风险指标 / manifest+compare / 基础研究报告）；M2=研究深度（Monte Carlo / 完整 market regime / Experiment Tracking / **SL+Roll 第一批** / Tail Risk 深化）；M3=数据真实性（Real Historical Option Chain / Bid·Ask / 流动性 / Early Assignment / 真实保证金 / **Synthetic vs Real Fidelity Study**）· 新增模块 `analysis` / `vol` / `research`（`backtest`/`report` 不单独立项，引擎核心零改动，§15 AC-11）· 双基准立项（Price / Total Return B&H，§6.4）· MDD 约束"无可行解"显式输出行为 · sweep 默认 metrics-first（`--save-all` 可选）· 研究声明原则（参数搜索结果 ≠ 未来预测，§17.2）· 新增 §15 M1-C Acceptance Criteria、§16 M2/M3 高层范围、§17 金融假设登记与研究效度。

---

## 0. 阅读指南

- §1 项目边界：做什么、不做什么、怎么算完成。
- §2 模块拆分：14 个模块的职责与边界，以及"用户 12 项需求 → 模块"的覆盖关系。
- §3 设计决策点：17 个决策，每个给出选项、优缺点、推荐方案与理由。
- §4 领域模型与数据流：**Raw/Derived 数据边界、核心对象的数据结构与生命周期、每日事件序列**。
- §5~§7：Sell Put 策略参数（M0 简化版）、指标（含 IV Rank/RV 与 §6.4 双基准）、测试策略。
- §8 里程碑：M0~M4 每步的交付物与验收点（v0.7 重排：M1-C 研究能力 / M2 研究深度 / M3 数据真实性）。
- §10 决策记录：唯一事实来源；§11 列出仍待你决策的问题。
- §15 M1-C 需求与验收标准（v0.7，十五项）；§16 M2/M3 高层范围；§17 金融假设登记与研究效度。

---

## 1. 项目定位与边界

### 1.1 定位原则

1. **核心是库（library），不是 GUI**。平台主体是一组可导入、可测试的 Python 模块；Notebook / CLI / 未来的 UI 都只是薄壳。
2. **研究平台，不是交易系统**。不做实时行情、真实下单、券商对接；这些永远在库核心之外。
3. **策略可插拔是第一架构目标**。新增 Covered Call / Put Spread / Wheel 只应新增一个策略类 + 参数 schema，不改引擎、不改组合层。
4. **复现是底线**。同配置 + 同种子 ⇒ 结果逐位一致；每次运行产出完整可追溯的实验目录。

### 1.2 Phase 1 范围（In Scope）

- **数据**：标的日线历史（免费源）+ 合成数据生成器（开发与金标准测试）+ 期权 EOD 链（可插拔 Provider，真实付费数据在 **M3** 接入，v0.7 决策）。
- **标的**：单 run 单标的；两类基准标的均支持 —— ETF（SPY/QQQ，美式、实物结算、可指派）与指数（SPX，欧式、现金结算），对应两种定价/结算风格。
- **策略**：M0/M1 只实现 Sell Put **基础规则**（DTE 30 / delta 0.20 / 止盈 50% / DTE≤3 强制退出）；**Stop Loss 与 Roll 属 M2 策略增强**，因它们显著增加 position state machine 与 execution 复杂度。
- **账户模拟**：Cash / 期权仓位 / 股票仓位（指派产生）/ 保证金 / 逐日盯市 PnL / 归因 / Greeks 聚合。
- **模拟引擎**：日频事件驱动；到期结算 + 简化提前指派模型（可选，默认关）；指派后默认次日开盘卖出股票（Wheel 属后续阶段）。
- **历史回测**：单标的、日频；网格参数 sweep；结果落盘 + 指标 + 图表 + HTML 报告。
- **参数研究（M1-C）**：策略参数配置化、网格 sweep、固定 train/test（2005–2018 / 2019–2024）、RV 市场环境分桶、MDD 约束筛选视图、manifest + compare、双基准（§6.4）、初步研究报告。
- **Monte Carlo**（M2）：模拟对象（underlying returns / volatility / option prices / strategy P&L）、方法（bootstrap / 参数化厚尾）与 IV 建模在 M2 立项时单独设计（v0.7 决策，§16）；M0 已交付 seeded GBM 路径生成器（`mc.py`）。
- **工程**：类型标注、Pydantic 配置校验、pytest（定价对拍 + 守恒不变量 + 金标准场景 + 防前视）、确定性随机种子。

### 1.3 明确排除（Out of Scope，Phase 1 不做）

| 排除项 | 说明 |
|---|---|
| 实时行情 / 真实下单 / 券商 API | 非研究平台职责；未来可作独立扩展包，不侵入核心 |
| 多标的组合回测（含相关性建模） | 架构预留（Portfolio 支持多腿多到期），阶段一只跑单标的 |
| 分钟级 / 盘中回测 | 引擎按"交易日事件"抽象，未来可升级，本阶段不承诺 |
| 精细公司行动（拆股除外） | 依赖数据源提供的调整后价格 |
| 税务优化、多币种、保证金利息 | 忽略或留常量参数（利率可配） |
| ML 信号、自动参数优化器 | 防止过拟合误导；只有网格 sweep；**MDD 约束筛选为筛选工具而非优化器，不构成预测**（v0.7，§17.2） |
| 完备的提前行权微观模型 | 阶段一用到期规则 + 可选简化概率模型（见 D12） |
| Stop Loss / Roll | **M2 再实现**（见 §5）；M0/M1 刻意保持简单 |

### 1.4 Phase 1 验收标准

1. 对任一支持标的跑通完整历史回测：净值曲线、指标表、逐笔清单、Greeks 时序、HTML 报告。
2. **金标准场景**：手工可验算的 CSP 到期指派全流程与引擎输出一致（测试写死期望值）。
3. **确定性**：同 config + seed 两次运行结果完全一致。
4. **扩展性证明**：不动引擎核心，新增一个演示策略（如 Covered Call）即可运行（作为架构验收测试）。
5. **MC 统计 sanity check**：常数波动率 + 持有到期的 CSP 策略，模拟平均 PnL 与真实测度解析期望（附录 C.4）在置信区间内一致。

---

## 2. 模块拆分

### 2.1 需求 → 模块覆盖表

| # | 用户需求 | 承担模块 |
|---|---|---|
| 1 | 历史市场数据 | `data`（Provider + Cache） |
| 2 | 期权数据 | `data`（链规范化；IV/Greeks 由引擎计算，见 §4.0） |
| 3 | 策略定义 | `strategy` |
| 4 | 参数配置 | `config` |
| 5 | 模拟下单与成交 | `execution` |
| 6 | Portfolio / Position / Cash | `portfolio` |
| 7 | PnL 计算 | `portfolio`（逐日盯市）+ `analysis`（汇总/归因） |
| 8 | 保证金计算 | `margin` |
| 9 | Greeks / IV | `pricing` + `vol` |
| 10 | 历史回测 | `sim`（事件引擎）+ `backtest`（编排） |
| 11 | Monte Carlo | `mc` |
| 12 | 结果分析与可视化 | `analysis` + `report` |

### 2.2 模块总览与依赖方向

```
                    ┌─────────────┐
                    │   config    │  类型化配置 / sweep
                    └──────┬──────┘
        ┌──────────────────┼──────────────────────┐
        ▼                  ▼                      ▼
 ┌────────────┐     ┌────────────┐        ┌──────────────┐
 │    data    │     │ instruments│        │  execution   │
 │ Provider/  │────▶│ OptionSpec │        │ Fill/滑点/费 │
 │ Cache/日历 │     └─────┬──────┘        └──────────────┘
 └─────┬──────┘           ▼
       ▼            ┌────────────┐        ┌──────────────┐
 ┌────────────┐     │  pricing   │◀──────▶│     vol      │
 │   margin   │     │EngineABC   │        │ HV/IV曲面/   │
 └─────┬──────┘     │BS/CRR/Gk   │        │ IV Rank/RV   │
       ▼            └─────┬──────┘        └──────┬───────┘
       ▼                  ▼                      ▼
 ┌────────────┐     ┌────────────┐        ┌──────────────┐
 │ portfolio  │◀────│    sim     │◀──────▶│      mc      │
 │ 仓位/PnL   │     │ 事件引擎   │        │ 路径/向量化  │
 └─────┬──────┘     └─────┬──────┘        └──────────────┘
       │                  │
       ▼                  ▼
 ┌────────────┐     ┌────────────┐
 │  strategy  │     │  research  │  编排: run/sweep/train-test/实验目录
 └────────────┘     └─────┬──────┘
                          ▼
                   ┌────────────┐   ┌────────────┐
                   │  analysis  │──▶│   report   │  指标/图表/HTML
                   └────────────┘   └────────────┘
```

**依赖规则**：只允许下层被上层依赖（linter 约束 import 方向）；`sim` 是历史回测与 MC 共用的唯一事件语义核心。

**v0.7 调整**：M1-C 新增 `research`（承接原 `backtest` 编排职责）、`analysis`、`vol` 三模块；`backtest` / `report` 不单独立项（报告由 `scripts/` 承担）。研究层只重复调用引擎、不侵入引擎（§15 AC-11）。

### 2.3 各模块职责与关键接口（伪代码）

| 模块 | 职责 | 关键类型 / 函数 |
|---|---|---|
| `config` | 全部配置的 Pydantic v2 schema；YAML 加载/导出；sweep 网格展开；`version` 字段做演进管理 | `BacktestConfig`, `DataConfig`, `StrategyConfig`, `SimConfig`, `MCConfig`; `load(path) -> BacktestConfig`; `expand_sweep(cfg) -> list[BacktestConfig]` |
| `data` | **Raw 数据**接入、规范化与缓存（Raw/Derived 边界见 §4.0）；交易日历 | `MarketDataProvider`（ABC）：`sessions()`、`close_snapshot(day, extra_strikes)`、`open_snapshot(...)`、`expiries_available(day)`；实现：`SyntheticProvider`（全合成）、`HybridProvider`（真实标的日线 + 合成链）；真实标的价格三级获取 `load_price_series`（本地缓存 → yfinance → 手动 CSV）；M3 新增 `HistoricalOptionProvider`（真实期权链，§16.2） |
| `instruments` | 期权合约与日历静态信息 | `OptionSpec(underlying, expiry, strike, right, style, multiplier, settlement)`；到期日生成（月度第 3 周五 / 周频，可配）；OCC 代码解析（备用） |
| `pricing` | **PricingEngine 抽象**与实现、风险指标、IV 求解、分红模型 | `PricingEngine`（ABC）：`price(snapshot_ctx, spec) -> PriceResult{Greeks}`；实现：`BlackScholesEngine`（欧式）、`CRRBinomialEngine`（美式，默认 200 步）；`DividendModel`（ABC）：`yield_rate(ts) -> float`（预留 `dividend_schedule(ts, horizon)`）；实现：`ContinuousYieldDividendModel`（M0，预留 `DiscreteDividendModel`）；`implied_vol(market_price, ...) -> σ`；`forward(S,T,r,q)` |
| `vol` | 波动率统计（M1-C：IV Rank / RV / RV 分桶）；IV 曲面模型属 M2+ | `hist_vol(returns, model=close2close|ewma) -> σ`；`realized_vol(returns, window=20)`；`iv_rank(iv_series, window=252)`、`iv_percentile(...)`、`rv_bucket(...)`；`IVSurface`（ABC，M2+）：`FittedSkewSurface`（M2）、`ConstantSurface`；M4 候选：SVI |
| `strategy` | 策略定义与下单意图生成 | `Strategy`（ABC）：`params_schema: type[BaseModel]`、`on_open(ctx, state) -> list[OrderIntent]`、可选 `on_close(...)`；实现：`SellPutStrategy`（§5）；后续 `CoveredCallStrategy`、`WheelStrategy`（SellPut/CoveredCall 状态机） |
| `execution` | OrderIntent → Order → Fill | `OrderIntent`, `Order`, `Fill`（§4.1）；`FillModel`（`next_open_mid` / `close_mid`）+ 滑点（bps）+ 手续费（每合约 + 每单） |
| `portfolio` | 账户状态与逐日会计 | `Cash`, `OptionPosition`, `EquityPosition`, `Portfolio`, `PortfolioState`（§4.1）；`mark_to_market(ctx)`；每日 PnL 分解（附录 B）；事件 `open/close/expire/assign`；`check_invariants()`（资金守恒断言） |
| `margin` | 保证金需求与账户约束 | `MarginModel`（ABC）：`requirement(pos, market) -> float`；实现：`CashSecuredMargin`、`SimplifiedRegTMargin`（附录 A；**research approximation，非券商 Reg-T 完整复现**）；`MarginPolicy`: `reject`（拒绝开仓）/ `liquidate`（次日开盘强平） |
| `sim` | 日频事件引擎（确定性核心） | `run_simulation(cfg, data, strategy, portfolio) -> list[PortfolioState]`；每日事件序列见 §4.2 |
| `mc` | Monte Carlo 路径模拟（M2；M0 已交付 seeded GBM 路径生成器，完整引擎 M2 立项设计，§16.1） | `generate_paths(seed, n, days, μ, σ) -> (n, days) ndarray`；未来：跨路径定价与事件执行；汇总统计（终值分位、VaR/CVaR、回撤分布、指派率、保证金缺口率） |
| `research` | 编排层（M1-C 新增；原 `backtest` 模块**不单独立项**） | `sweep(grid, base_cfg, train_window) -> list[SweepResult]`（多进程）；`split(train_end)`；`write_manifest(...)`；MDD 约束筛选视图（无可行解显式输出）；`compare(runs)` |
| `analysis` | 指标与归因（M1-C 实现；纯函数，输入 states/trades/prices，无 I/O） | §6 全部指标（含 RV 分桶统计、§6.4 双基准）；PnL 归因表；按**实际入场** DTE/delta 分桶；train/test 分段指标 |
| `report` | 可视化与导出（**不单独立项**，由 `scripts/` 承担） | M1-C：sweep 热图报告 + 研究 Markdown 报告（含 §17.3 效度声明）；现有 `build_report.py` 保持 |

### 2.4 仓库目录结构（`options-platform/`，即未来 git 根）

```
options-platform/
├─ docs/technical-spec.md
├─ src/sellput/            # Python 包（src layout），14 个模块按 §2.3 组织
├─ configs/                # 示例回测配置（yaml）
├─ notebooks/              # 研究示例（每个策略/主题一个）
├─ tests/                  # pytest：对拍 / 不变量 / 金标准场景 / 防前视
├─ data/                   # 本地数据缓存（gitignore）
├─ runs/                   # 实验输出（gitignore）
└─ pyproject.toml
```

> 说明：本平台与工作区中其它无关目录相互独立；git 仓库根目录即本仓库根（`options-platform/`，即本文档所在的仓库根）。

---

## 3. 设计决策点（D1–D17）

### A. 平台形态与技术

#### D1 编程语言与生态
- **A. Python 3.12+（推荐）** — 优点：量化生态无可替代（numpy/scipy/pandas/polars/plotly），原型迭代最快；缺点：纯 Python 循环慢（对策见 D10）。
- **B. TypeScript 全栈** — 优点：前后端同构；缺点：量化库生态弱，自研定价成本高。
- **C. 混合** — 优点：兼顾计算与界面；缺点：阶段一引入前端成本，违背"库优先"。

**推荐**：**A**。未来若要 Web UI，用 FastAPI 包一层即可。

#### D2 交互形态
- **A. Jupyter + CLI 起步（✅ 已确认）** — 研究范式天然契合，零 UI 维护成本；输出 HTML 报告 + 交互图落盘。
- **B. Streamlit 薄 UI** — M3 后按需加（只 import 核心库，不改架构）。
- **C. 全栈 Web** — 阶段一性价比低，排除。

#### D3 代码组织
- **A. 单仓库单包，src layout（推荐）** — 模块边界靠目录 + import 规则约束。
- **B/C. monorepo 多包 / 多仓库** — 阶段一过度设计。

**推荐**：**A**。

### B. 数据

#### D4 历史数据源与预算
**问题**：标的行情免费可得，但**期权历史链是最大的外部依赖**。

| 选项 | 数据能力 | 成本量级 | 备注 |
|---|---|---|---|
| **A. 免费 + 合成（✅ 已确认起步）** | yfinance 标的日线（可靠）；期权链仅"当前 + 极短历史"，无法做历史回测 | 0 | 合成数据（可控 GBM + 自造链）用于开发与金标准测试 |
| **B. CBOE DataShop** | 官方 EOD 期权链（按标的/年份购买），有学术折扣 | 低~中 | M2 候选 |
| **C. ThetaData** | 期权专用（EOD/盘中、链+greeks+OI），订阅制 | 中 | M2 候选 |
| **D. Polygon.io** | options aggregates/trades flat files | 中~高 | 阶段一过量 |
| **E. OptionMetrics / ORATS** | 机构级 | 高 | 阶段一过量 |
| **F. 本地 CSV 导入** | 自备 | 0 | 兜底通道 |

参考：[ThetaData Subscriptions](https://http-docs.thetadata.us/Articles/Getting-Started/Subscriptions.html)、[CBOE DataShop Option EOD Summary](https://datashop.cboe.com/option-eod-summary)（价格以官网当前为准）。

**结论**：A 起步；**M3-A 前**再议真实数据采购（v0.7：供应商重估标准见 §16.2）。

#### D5 数据存储
- **A. DuckDB + Parquet（推荐）** — 列存压缩、嵌入式零运维、SQL 直查 Parquet 目录。
- **B. SQLite** — 行存，时序分析慢。
- **C. 纯 CSV** — 仅作导入导出格式。

**推荐**：**A**，库内统一 `DataCache` 接口。

### C. 定价与波动率

#### D6 定价模型与 PricingEngine 抽象
**问题**：两类标的的行权风格不同，定价引擎如何组织。

- **SPX：European / cash settled → Black-Scholes**（解析 + 解析 Greeks）。
- **SPY：American / physically settled → CRR 二叉树**（默认 200 步，含连续分红 q）。

**抽象**：`PricingEngine`（ABC），`price(ctx, spec) -> PriceResult{Greeks}`；实现 `BlackScholesEngine` / `CRRBinomialEngine`，按 `pricing.engine: auto|bs|crr` 选择（auto：SPX→bs，SPY→crr）。

**明确声明（写入代码文档与报告）**：**Black-Scholes + 连续分红 q 对 SPY 只是近似，不是严格的美式期权定价**。配置允许强制 `engine: bs` 做快速近似回测，但报告必须标注"近似定价"。

**推荐**：M0 同时实现 BS 与 CRR 两个引擎（SPY 严格定价需要 CRR）；BS 引擎同时是 MC 向量化定价（M2）的载体。

**✅ 已确认**：CRR 进入 M0；`engine: auto`（SPX→BS、SPY→CRR），BS 可作 SPY 近似/回退；CRR 第一版保持标准实现，**不加入高级 early-exercise 优化**；`summary.json` 与报告明确标注所用模型类型。

#### D7 波动率 / IV 建模
- **A. 历史回测用市场 IV，MC 用拟合简化曲面（推荐）** — 回测直接用当日链市场 IV 定价持仓；MC 用从历史链拟合的"ATM 期限 + 线性 skew"曲面。
- **B. 常数/EWMA 历史波动率定价** — 数据缺 IV 时的回退。
- **C. 完整 SVI / SABR 曲面** — M4 候选。

**推荐**：**A**（回退 B；`IVSurface` ABC 保证 C 可后插）。

#### D17 分红建模
**问题**：SPY 是离散现金分红，SPX 适合连续分红率 q；架构上如何兼顾。

- **M0 结论（✅ 已确认）**：使用 **continuous dividend yield q**（可配或从 Provider 自动估计）。
- **架构要求（✅ 已确认）**：分红模型抽象为可替换组件 `DividendModel`（ABC）：
  - M0 接口：`yield_rate(ts) -> float`（连续 q）；
  - 预留接口：`dividend_schedule(ts, horizon) -> list[(ex_date, amount)]`，供未来 `DiscreteDividendModel` 实现。
  - **不在架构层面假设 ETF 与指数永远使用同一种分红模型**：每个标的可绑定不同的 `DividendModel` 实例（配置项 `pricing.dividend.model`）。
- 连续 q 对临近除息日的短期期权有偏差（对 30–45 DTE 影响很小）；M4 扩展个股时可引入离散分红（仅与 CRR 兼容，BS 解析不适用）。

### D. 模拟与回测

#### D8 引擎形态
- **A. 事件驱动，日频（推荐）** — 到期/行权/保证金等状态机语义清晰；10 年日频 <10s 目标。
- **B. 全向量化** — roll/行权等分支逻辑难表达，策略扩展性差。
- **C. 现成框架（backtrader/zipline）** — 期权链/保证金/到期事件需大量 hack，且已停止活跃维护。

**推荐**：**A**（借鉴 zipline 事件模型思想，引擎自研轻量）；MC 层做跨路径向量化（D10）。

#### D9 成交假设与防前视纪律
**问题**：日频数据下以什么价格成交，如何杜绝未来数据。

- **成交假设（✅ 已确认）**：收盘信号 → **次日开盘 mid 成交** + 5 bps 滑点 + 手续费（每合约 $0.65 + 每单最低 $1，全部可配）。
- **防前视纪律（✅ 已确认，强制）**：
  1. **Signal generation 与 execution 必须使用不同时点的 `MarketSnapshot`**：信号只读 `CloseSnapshot(t−1)`；成交只用 `OpenSnapshot(t)`。
  2. **禁止回测中使用未来数据**：任何模块不得访问晚于当前事件时刻的数据；`MarketSnapshot` 不可变且带时间戳，违规在代码评审与测试中拦截。
  3. 配套测试见 §7（数据平移测试 + 快照分离断言）。

#### D10 Monte Carlo 定价与执行方案（M2，立项时单独设计，v0.7 §16.1）
- **A. IV 曲面 + 解析 BS 向量化定价（推荐）** — 跨路径批量 `(S,K,T,σ)` 数组一次计算；1000 路径 × 252 天目标 <5 分钟。
- **B. 逐路径逐日 CRR** — 量级不可行，除非 numba 重写。
- **C. 常数波动率 GBM** — 无 skew，只做校验基准（附录 C.4）。

**推荐**：**A 为主 + C 做校验**。MC 目标是分布与尾部风险，不是逐点精确定价。

### E. 账户与风控

#### D11 保证金模型
- **A. Simplified Reg-T-style + Cash-Secured 双实现，配置切换（✅ 已确认）** — 默认 Simplified Reg-T-style（附录 A.1，ETF 20% / 宽基指数 15% 系数）；`csp` 可切。
- **B. Portfolio Margin** — M4+ 候选。
- **C. 只做 CSP** — 与真实账户差距大。

**结论**：**A**；保证金不足策略（拒绝开仓 vs 次日强平）可配，默认拒绝。
**命名与声明（✅ 已确认）**：模型命名为 **Simplified Reg-T-style Margin Model**；**不声称对真实券商 Reg-T / broker-specific margin 的完整复现**；代码 docstring 与报告均标注 "research approximation"。

#### D12 行权与指派处理
- **A. 到期确定性规则 + 可选简化提前指派（推荐）** — 到期 ITM 即行权/指派（SPX 现金结算按收盘价现金轧差；SPY 实物交割产生股票仓位）；提前指派为可选 Bernoulli 模型（默认关）；指派后按 `assignment_policy` 处理（默认 `sell_next_open`）。
- **B. 仅到期规则** — 深度 ITM 美式实际存在提前指派，结果略偏乐观。
- **C. 精细提前行权模型** — M4+ 候选。

**推荐**：**A**。**SPX 结算简化（✅ 已确认）**：M0 统一按**收盘价现金结算**，忽略月度 AM / 周度 PM 等结算差异 —— **这是 M0 的简化模型，不代表真实 SPX 产品的全部 settlement convention**，在代码文档与报告中显式注明。

#### D13 仓位与资金管理
- **A. 按可用资金比例分配（推荐默认）** — 合约数 = ⌊可用资金 × 50% ÷ 单合约保证金占用⌋，不足 1 张不开仓。
- **B. 固定合约数** — 可选。
- **C. 目标 delta 组合仓位** — 阶段一过度设计。

**推荐**：**A 默认 + B 可选**。

### F. 研究体验与工程

#### D14 参数研究与样本外纪律
**问题**：sweep 如何防过拟合。

- **A. 内置网格 sweep + 强制样本外切分（✅ 已确认）** — 默认 70/30 时间切分，并明确三条纪律：
  1. **train set 可用于参数搜索**（sweep、分桶观察、规则调试）；
  2. **test set 仅用于最终 evaluation**（选定的最终参数在 test 上跑一次，产出报告数字）；
  3. **不允许根据 test set 结果回头选择最佳参数**（sweep 对比视图只允许在 train 上排序；`sellput compare` 对 test 段默认隐藏排序功能，仅展示最终报告）。
- **B. 仅单次运行** — 手工跑参数太低效。
- **C. 自动优化器** — 过拟合放大器，不引入。

**推荐**：**A**，纪律写入 CLI 行为而非仅文档。

#### D15 实验管理与复现
- **A. 每次运行产出实验目录（推荐）** — `runs/<时间戳>_<标签>/`（§4.3）；`sellput compare` 对比。
- **B. 仅内存打印** — 违背复现底线。

**推荐**：**A**。

#### D16 工程与测试基础设施
- **A. uv + pytest + ruff（推荐）** — Python ≥3.12；依赖：numpy、scipy、pandas、pydantic≥2、duckdb、pyarrow、plotly、yfinance、pandas_market_calendars、pytest、ruff；可选：polars、numba、jupyterlab、streamlit。
- **B. pip + venv** — 依赖解析与迁移体验差。

**推荐**：**A**。

---

## 4. 领域模型与数据流

### 4.0 Raw Market Data 与 Derived Data 的边界

**原则（✅ 已确认）**：引擎只消费 Raw 数据；**IV 与 Greeks 一律由 Pricing/Analytics Engine 计算，不作为核心原始数据依赖**。

**Raw Market Data（数据层契约，Provider 必须产出）**，每条期权报价行至少包含：

| 字段 | 说明 |
|---|---|
| `timestamp` | 报价时点（阶段一：交易日 + OPEN/CLOSE session） |
| `underlying_price` | 标的价格 |
| `bid` / `ask` / `last` | 期权买卖价与最新成交价 |
| `strike` / `expiration` / `option_type` | 行权价 / 到期日 / C|P |
| `volume` / `open_interest` | 成交量 / 未平仓量 |
| 元数据 | `symbol`、`multiplier`、`settlement`（cash|physical）、`style`（european|american） |

**Derived Data（由平台计算，可重算）**：

| 数据 | 计算模块 | 说明 |
|---|---|---|
| IV | `pricing.implied_vol` | 由 mid/last + 定价引擎反解 |
| Greeks | `pricing`（解析/数值） | 由引擎计算 |
| HV / RV | `vol` | 历史/已实现波动率 |
| IV Rank / IV Percentile | `vol` | 由 IV 序列统计（§6） |
| IV 曲面参数 | `vol`（M2） | 由链数据拟合 |

**边界规则**：
1. 缓存层只持久化 Raw 数据；Derived 数据可按需落盘为辅助列，但必须带 provenance（定价引擎类型 + 包版本），且**始终可自 Raw 重算**。
2. Provider 若自带 IV/Greeks（如 ThetaData），仅存为对照列（`iv_provider` 等），**默认不参与定价与盯市**。
3. `MarketSnapshot` 中的 `rate`、`dividend_yield` 属上下文派生量，由配置/`DividendModel` 注入，不走期权报价数据。

### 4.1 核心对象模型（数据结构与生命周期）

#### 对象关系总览

```
MarketSnapshot (data 层, 不可变, 带时点)
   │ 只读
   ▼
Strategy ──OrderIntent──▶ Execution ──Order──▶ Fill (不可变成交记录)
   ▲                        │                     │ apply
   │ 只读 state             │                     ▼
   └──────────── Portfolio (持有 Positions / Cash) ◀┘
                       │ 每日事件: expire / assign / margin
                       ▼
                 PortfolioState (每日快照) ──▶ equity curve / analysis
```

#### 各对象定义

**`MarketSnapshot`**（不可变值对象；每个交易日两种时点）
```python
MarketSnapshot{
  date: Date, session: OPEN | CLOSE,
  underlier: UnderlierQuote{symbol, price, prev_close},
  option_quotes: dict[OptionId, OptionQuote{bid, ask, last, volume, open_interest}],
  rate: float,               # 无风险利率（配置注入）
  dividend_yield: float,     # DividendModel.yield_rate(date)
}
# OptionId = (expiry, strike, right)
```
- **生命周期**：data 层按 `(date, session)` 物化 → 整个 run 内不可变、可缓存 → run 结束随缓存策略处置。
- **铁律**：信号只读 `CloseSnapshot(t−1)`；成交只用 `OpenSnapshot(t)`（§4.2 步骤 1/2）。

**`OrderIntent`**（策略输出，无副作用）
```python
OrderIntent{
  action: OPEN | CLOSE,
  option: OptionSpec,            # 开仓时由策略在 t−1 收盘链上选定具体合约
  contracts: int,
  limit: float | None,           # 阶段一为 None（按 FillModel 成交）
  reason: ENTRY | TAKE_PROFIT | DTE_EXIT | ASSIGNMENT_LIQUIDATION | ROLL | STOP_LOSS,  # ROLL/STOP 为 M2 预留
}
```
- **生命周期**：`Strategy.on_open` 产生 → Execution 消费 → 结束。策略不直接改账户状态。

**`Order`**（Execution 簿记）
```python
Order{id, intent: OrderIntent, ts, status: PENDING | FILLED | REJECTED, reject_reason}
```
- **生命周期**：受理 → 校验（保证金/资金）→ FILLED 或 REJECTED 归档。

**`Fill`**（不可变成交记录）
```python
Fill{id, order_id, ts, price, contracts, commission, slippage_bps, snapshot: OpenSnapshot(t)}
```
- `price = OpenSnapshot(t).mid × (1 ∓ slippage)`（买卖方向取不利侧）。
- **生命周期**：成交时创建，永久归档；任何对象不得事后修改。

**`Position`**（组合持仓腿）
```python
OptionPosition{id, spec: OptionSpec, qty: int,       # 空头为负
               avg_price, opened_fill_id, realized_pnl}
EquityPosition{symbol, qty, avg_cost}                # 指派产生；默认次日开盘卖出
```
- **生命周期状态机**：`OPEN`（由 OPEN Fill 创建）→ 每日盯市（unrealized）→ 终结于三途：
  - `CLOSED`：CLOSE Fill；
  - `EXPIRED`：到期 OTM，权利金全收（空头）；
  - `ASSIGNED`：到期 ITM 行权/指派 → 转化为 `EquityPosition`（SPY）或现金轧差（SPX）。
- 终结即归档为 `Trade`；同一 `(underlying, expiry, strike, right)` 的 Position 按 FIFO 配对。

**`Trade`**（开平配对，分析单元）
```python
Trade{id, spec, open_fill, close_fill | expiry | assign,
      entry_date, exit_date, pnl, commissions, exit_reason,
      entry_iv, exit_iv, dte_at_entry, delta_at_entry, iv_rank_at_entry}
```
- **生命周期**：Position 终结时生成 → 写入 `trades.parquet`；分析分桶（DTE/delta/IV Rank）以此为单位。

**`PortfolioState`**（每日收盘快照，不可变）
```python
PortfolioState{
  date, cash, positions: list[Position],
  margin_used, margin_required,
  equity, greeks: Greeks{delta,gamma,theta,vega,rho},
  attribution: dict[str, float],      # 附录 B 分解
  benchmark_price: float,
}
```
- **生命周期**：每日事件序列（§4.2）结束后生成一份 → 串联成 equity curve；MC 模式为每 `(date, path)` 一份。

#### 生命周期汇总表

| 对象 | 创建 | 变更 | 终结/归档 |
|---|---|---|---|
| `MarketSnapshot` | data 层按 `(date, session)` 物化 | 不可变 | run 结束（可缓存） |
| `OrderIntent` | `Strategy.on_open` | 无 | 被 Execution 消费 |
| `Order` | Execution 受理 | status 推进 | FILLED / REJECTED 归档 |
| `Fill` | Execution 成交 | 不可变 | 永久归档 |
| `Position` | OPEN Fill / 指派 | 逐日盯市、qty 调整 | CLOSE / EXPIRE / ASSIGN → 归档为 `Trade` |
| `Trade` | Position 终结时配对生成 | 无 | 写入 `trades.parquet` |
| `PortfolioState` | 每日收盘快照 | 不可变 | 写入 `equity_curve.parquet` |

### 4.2 每日事件序列（确定性顺序）

对每个交易日 `t`：

1. **开盘决策（信号生成）**：`Strategy.on_open` 只读 `CloseSnapshot(t−1)` 与当前 `PortfolioState`，产出 `OrderIntent`。**禁止访问 t 日任何数据**。
2. **成交（执行）**：Execution 按 `FillModel` 用 **`OpenSnapshot(t)`** 的 mid ± 滑点成交。信号与执行使用不同时点快照（§D9）。若上日有强平挂单，优先执行强平。
3. **到期 / 指派处理**（收盘后，用 `CloseSnapshot(t)`）：到期 ITM ⇒ 行权/指派（SPX 现金轧差；SPY 实物交割）；提前指派模型（可选，默认关）对深度 ITM 美式空头按概率 p 指派。
4. **收盘盯市**：全部持仓按 `CloseSnapshot(t)` 定价（市场 IV 优先，缺则曲面/EWMA 回退），计算逐仓与组合 Greeks、PnL 分解（附录 B）。
5. **保证金检查**：`margin_used > 可用资金` ⇒ 按 `MarginPolicy`（默认拒绝新开仓；`liquidate` 则挂次日开盘强平单）。
6. **快照**：生成 `PortfolioState(t)`。

MC 模式（M2）复用同一事件序列，1~5 步对全部路径批量向量化执行（D10），随机数只用 seed 固定的 `numpy.random.Generator`。

### 4.3 结果 Schema 与实验目录

```
runs/20260115-093000_spy-d30_d20/
├─ manifest.json         # v0.7 必需：完整配置 + git commit + 包版本 + 数据集指纹 + 指标（§15 F8）
├─ metrics.json          # §6 全部指标（train/test/full 三段）+ 配置摘要 + 定价引擎标注（近似定价显式声明）
├─ equity_curve.parquet  # date, equity, cash, positions_value, margin_used/required,
│                        #   greeks 汇总, attribution, benchmark_equity
├─ trades.parquet        # 每笔 Trade（含 exit_reason、entry_iv、iv_rank_at_entry 等分桶字段）
├─ greeks_daily.parquet  # 可选（save_snapshots: true）
└─ report.html           # Plotly 图表 + 指标表
```

MC 额外输出：`mc_paths_summary.parquet`（每条路径终值/最大回撤/是否指派/是否保证金缺口）。

> v0.7：当前 M1-A/M1-B 实际产出为 CSV（states/trades/prices）+ summary.txt + HTML；M1-C 起由 research 层统一写入 manifest.json / metrics.json（sweep 默认 metrics-first，全量 artifacts 经 `--save-all` 可选，§15 N4）。

### 4.4 完整配置示例（M0/M1 目标形态）

```yaml
version: 1
run:
  name: "spy-d30-d20-baseline"
  seed: 42
  save_snapshots: false

data:
  provider: hybrid              # 实现支持：synthetic（全合成）| hybrid（真实标的 + 合成链）
                                # （M3：真实历史期权链 Provider，届时另行立项，§16.2）
  symbol: SPY
  start: "2015-01-01"
  end: "2025-12-31"
  hybrid:                       # provider=hybrid 时的数据获取参数（M1-A 三级获取）
    vol_window: 20              # 驱动合成链 iv_atm 的滚动已实现波动率窗口（交易日）
    cache_dir: data             # 本地缓存目录（data/<symbol>_daily.csv）
    csv_path: null              # 手动 CSV 兜底；null = 自动扫描 cache_dir 下的 *.csv
    buffer_days: 70             # 起点前补数据（滚动波动率 / 前收 / 波动率分位预热）
    offline: false              # true = 只用缓存或手动 CSV，不联网

market:
  rate: 0.04                    # 无风险利率

pricing:
  engine: auto                  # auto: SPX→bs / SPY→crr；可强制 bs（SPY 近似，报告标注）| crr
  binomial_steps: 200
  dividend:
    model: continuous_yield     # M0；未来: discrete_schedule
    yield_source: auto          # auto（Provider 估计）| float

simulation:
  fill: next_open_mid           # 收盘信号 → 次日开盘 mid 成交（D9）
  slippage_bps: 5               # 0.05%
  commission_per_contract: 0.65
  commission_per_order: 1.0
  interest_on_cash: 0.0
  margin_model: simplified_regt # simplified_regt | csp（research approximation，附录 A.1）
  margin_policy: reject         # reject | liquidate
  assignment_policy: sell_next_open
  early_assignment: false

account:
  starting_cash: 100_000
  sizing: {mode: pct_allocated, pct_allocated: 0.5, max_contracts: null}

strategy:
  type: sell_put
  params:                       # M0/M1 简化规则，见 §5
    dte_target: 30
    delta_target: 0.20
    profit_target_pct: 0.50
    dte_exit: 3
    entry_frequency: weekly
    max_open_positions: 1

sweep: {}                       # M1-C: {dte_target: [...], delta_target: [...], profit_target_pct: [...]}
split: {train_end: "2018-12-31"}  # M1-C 固定切分（train 2005–2018 / test 2019–2024）；D14 纪律
benchmark: {price_return: true, total_return: true}  # M1-C 双基准（§6.4）

monte_carlo: null               # M2（方法与 IV 建模待立项设计，§16）
```

---

## 5. Sell Put 策略规范

### 5.1 M0/M1 基础规则（✅ 已确认：刻意简单）

| 参数 | 默认 | 语义 |
|---|---|---|
| `dte_target` | 30 | 开仓目标剩余天数：在可用到期日中选择 DTE 最接近 30 的 |
| `delta_target` | 0.20 | 选行权价：取 delta 绝对值最接近目标的 OTM Put |
| `profit_target_pct` | 0.50 | 权利金回笼 50% 时平仓（开盘检查，按 t−1 收盘估值） |
| `dte_exit` | 3 | **剩余 DTE ≤ 3 强制退出**（未触发止盈也平仓离场） |
| `entry_frequency` | `weekly` | 每周首个交易日开盘检查；`when_free` = 有空位就开 |
| `max_open_positions` | 1 | 同时持有的独立空头 Put 腿数上限 |
| `sizing`（account 层） | 50% 资金分配 | 合约数向下取整；不足 1 张不开仓 |
| `assignment_policy`（sim 层） | `sell_next_open` | 指派后次日开盘卖出股票 |

**规则优先级**（同一天多条命中）：`DTE≤3 强制退出 > 止盈 50% > 新开仓`。

**v0.7（M1-C）**：合成链**保留月度到期结构**（不加周度，保持 M0/M1 历史结果可回归；周度到期属 M3 数据真实性议题，§16.2）。参数研究中 DTE 一律按**实际入场 `dte_at_entry`** 分桶分析，不得假定 target DTE 等于实际 DTE；合成链到期结构限制必须写入报告（§15 AC-10）。

**M0/M1 不实现（✅ 已确认，M2 引入）**：`stop_loss_multiple`（止损 2× 权利金）与 `roll`（滚动换仓）。理由：两者引入跨日持仓状态机与更复杂的执行/记账路径，先以最简规则跑通全链路。M2 引入后优先级变为：`止损 > 止盈 > roll > DTE 退出 > 新开仓`，接口已预留（`OrderIntent.reason: ROLL|STOP_LOSS`）。

### 5.2 Wheel 的预留方式（不在 Phase 1 实现）

`WheelStrategy` = 状态机：`sell_put` 状态复用 `SellPutStrategy` → 指派转入 `covered_call` 状态（复用 `CoveredCallStrategy`）→ 被 call 走回到 `sell_put`。`Strategy` 已支持跨日状态（`state` 参数），无需改引擎 —— 构成 §1.4 验收标准 4 的架构验证用例。

---

## 6. 指标与可视化清单

### 6.1 指标（`analysis` 模块，全部进入 `summary.json`）

| 类别 | 指标 |
|---|---|
| 收益 | 总收益、CAGR、年化波动率、Sharpe（rf 可配）、Sortino、Calmar |
| 回撤 | 最大回撤、最大回撤持续天数（与基准对比：超额收益、最大相对回撤） |
| 交易 | 交易笔数、胜率、平均盈利/亏损、Profit Factor、平均持仓 DTE、年均交易次数 |
| 效率 | 资本效率 = 累计权利金 / 平均保证金占用；ROC = 年化收益 / 平均占用保证金；保证金峰值占用率 |
| 波动率环境（✅ 新增） | **IV Rank**、**IV Percentile**、**Realized Volatility**（定义见 6.2） |
| 风险（含 MC） | 指派率、最大单笔亏损、MC：终值 P5/P1 分位、VaR95、CVaR95、回撤分布、保证金缺口概率、盈利概率 |
| 归因 | 每日 PnL 分解（附录 B）按 delta/theta/vega/残差累计 |

**指标范围封版声明**：以上清单为 Phase 1 最终指标集；后续新增指标需单独立项讨论，不随需求无限扩展。**v0.7 立项**：新增双基准（Price Return / Total Return B&H，§6.4），其余封版不变。

### 6.2 新增指标定义与分桶统计（✅ 已确认）

- **IV Rank**：`(IV_t − min(IV, 252d)) / (max(IV, 252d) − min(IV, 252d))`，IV 取 30 DTE ATM（近月 ATM 插值），窗口默认 252 交易日（可配）。
- **IV Percentile**：`P(IV_{252d} < IV_t)` —— 当前 IV 在 252 日窗口分布中的分位。
- **Realized Volatility**：`std(log_returns, window=20) × √252`，窗口默认 20d（60d 可选）。

**按 IV Rank 分层的策略表现**（`analysis` 输出，`report` 展示）：默认 4 档 —— `<25` / `25–50` / `50–75` / `≥75`；每笔 `Trade` 按 `iv_rank_at_entry` 归桶，输出分桶的：样本数、胜率、平均 PnL、资本效率、平均持仓 DTE。用于观察策略在不同波动率环境下的表现差异。

### 6.3 图表（`scripts/` 报告生成，Plotly）

净值曲线（vs 标的买入持有双基准）、回撤曲线、逐笔 PnL 分布直方图、持仓 Greeks 时序（delta/theta/vega）、保证金占用时序、IV（IV Rank）与已实现波动率对比、IV Rank 分桶收益箱线图、按实际入场 DTE/delta 分桶统计、MC：路径簇 + 分位带 + 终值分布直方图（M2）。

### 6.4 基准定义（v0.7 立项：双基准）

**决策（§10）**：Sell Put 收取权利金属收益型策略，仅用价格型基准会产生解释偏差，故 M1-C 起同时输出两种基准；两者定义必须写入报告。

| 基准 | 定义 | 数据 | 说明 |
|---|---|---|---|
| Price Return B&H | 期末收盘价 ÷ 期初收盘价 − 1（现有口径） | 同一 PriceSeries 收盘序列 | 与策略净值同口径直接比较 |
| Total Return B&H | 价格收益 + 现金分红再投资（除息日现金分红按**次一交易日开盘价**全额再投，无摩擦、无税） | `PriceSeries.dividends`（已有）+ 开盘价序列 | 在 **analysis 层**计算（`total_return_index(...)`），**不改引擎、不改策略** |

**最小设计原则（v0.7）**：

1. 基准指数为 analysis 层纯函数：输入 prices（open/close）与 dividends（{ex_date: per_share}），输出逐日总回报指数；两种基准的"超额收益"都进入 summary 与报告。
2. 再投约定固定为"除息日现金分红 ÷ 次一交易日开盘价 → 份额增加"，全程无手续费/税；约定写入报告。
3. 数据充分性：当前 `PriceSeries.dividends`（yfinance 真实分红 / CSV `Dividends` 列）足以支撑；若未来数据源缺失分红，**必须显式降级声明（回退价格型）而非静默简化**。
4. 引擎与 B&H 策略**零改动**（B&H 策略仍为分红不付现口径，作为策略对照保留；基准升级只发生在分析/报告层）。

---

## 7. 测试与验证策略

| 层 | 测试 |
|---|---|
| 定价对拍 | BS 与 Hull 教材算例一致；put-call parity；CRR 随步数收敛到 BS；`IV(price(IV))=IV` 往返一致；解析 Greeks vs 有限差分 |
| 引擎不变量 | 任意事件脚本后资金守恒：`cash + Σ持仓市值` 连续；强平后全部归现金；同 config+seed 结果逐位一致 |
| 金标准场景 | 手工验算写死在测试里：CSP 开仓 → 到期 OTM（权利金全收）/ ITM 指派（按行权价接货）→ 次日卖出；止盈触发值精确匹配 |
| 防前视 | ① 快照分离断言：信号只依赖 `CloseSnapshot(t−1)`，把 `OpenSnapshot(t)` 篡改为任意值不影响 `OrderIntent` 集合；② 数据整体平移测试：决策集不变，仅成交价随 FillModel 变化 |
| Derived 可重算 | 从 Raw 重算 IV/Greeks 与缓存 Derived 列一致（provenance 校验） |
| MC 校验 | 常数波动率 + 持有到期 CSP：模拟平均 PnL ≈ 真实测度解析期望（附录 C.4）在 95% CI 内；antithetic 方差下降 |
| 扩展性 | §1.4 验收 4：加 `CoveredCallStrategy` 无需改动 sim/portfolio/margin |

**性能说明（✅ 已确认）**：性能数值（10s / 5min）为 benchmark target，不是 correctness requirement —— 不同机器性能不应直接导致功能测试失败；benchmark 以 `@pytest.mark.benchmark` 标记的独立测试/脚本承载，默认测试运行排除。

---

## 8. 里程碑计划

| 里程碑 | 交付物 | 验收点 |
|---|---|---|
| **M0 垂直切片与正确性闸门（✅ 已完成）** | 仓库骨架；config；Raw/Derived 数据契约 + `SyntheticProvider`；核心对象；`pricing`（BS + CRR + Greeks + IV）；`DividendModel`；`instruments` + 日历；`execution`；`portfolio`；`margin`；`sim`；`SellPutStrategy` 四规则；`mc` seeded GBM 路径生成器；benchmark 脚本 | **§12 M0 Acceptance Criteria 全部通过** |
| **M1 历史回测全链路（✅ 已完成：M1-A / M1-B）** | M1-A：真实 SPY 日线（缓存/yfinance/CSV 三级）+ HybridProvider（合成链，20d 滚动 RV 驱动）+ 按日股息率 + 一键回测。M1-B：多策略架构（订单/Trade/组合/引擎泛化 + `BuyHoldStrategy` + `on_final` + CLI `--strategy`，§14） | §13 八项 / §14 八项验收全过；10 年回测 <10s（benchmark target） |
| **M1-C 基础研究能力（下一步实施）** | `analysis`（§6 全部指标 + §6.4 双基准）、`vol`（IV Rank / RV / RV 分桶）、`research`（sweep / train-test / manifest / MDD 约束筛选）；CLI 策略参数化；`run_sweep.py` / `compare_runs.py`；sweep 热图报告 + 研究 Markdown 报告 | **§15 十五项验收全过**；引擎核心 diff 为空（AC-11）；既有 93 测试 + 新增测试全绿 |
| **M2 研究深度** | Monte Carlo（模拟对象、方法与 IV 建模立项时单独设计，§16.1）；完整 market regime（bull/bear/sideways/高·低波动/极端事件）；Experiment Tracking（索引库 + 对比 UI）；**SL / Roll 第一批**（四臂对比研究）；Tail Risk 深化；（可选）walk-forward | 逐项单独立项（§16.1） |
| **M3 数据真实性** | Real Historical Option Chain（供应商按 §16.2 标准重估后接入）；Bid/Ask、流动性过滤；Early Assignment；更真实保证金；**Synthetic vs Real Fidelity Study** | 逐项单独立项（§16.2）；fidelity study 回答"模型假设是否改变研究结论" |
| **M4（预留）** | Covered Call → Wheel → Put Spread；SVI 曲面；离散分红 `DiscreteDividendModel`；可选 Streamlit UI；可选盘中数据 | 逐项单独立项 |

**性能说明（✅ 已确认）**：性能数值（10s / 5min）是 benchmark target，不是 correctness requirement —— 不同机器性能不应直接导致功能测试失败；benchmark 以独立脚本 + `@pytest.mark.benchmark` 标记测试承载，默认测试运行排除。

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 期权历史数据获取/成本（最大外部风险） | M3 卡住或回测失真 | Provider 抽象 + 合成数据先行；M3-A 前按 §16.2 标准重估供应商并小额订阅验证（v0.7 决策） |
| 回测过拟合（sweep 制造虚假结论） | 结论不可信 | train/test 纪律（D14）写入 CLI 行为；参数维度限制；实验全程留档 |
| Python 性能（MC） | 模拟不可用 | 跨路径向量化 + 多进程；numba 作逃生舱；性能目标写进验收 |
| 模型假设偏差（BS/GBM 无跳变、常数 r、**BS 近似 SPY 定价**） | 尾部风险低估、SPY 定价偏差 | 报告显式标注近似定价；敏感性分析 + 压力场景；SPY 默认 CRR；M4 可选 jump 模型 |
| 数据前视偏差 | 回测虚高 | 快照时点分离（D9）+ 防前视测试（§7） |
| Synthetic 结论外推（把合成假设下的数字当真实业绩） | 结论不可信、作品集可信度受损 | §17 假设登记 + 报告强制效度声明（§17.3）；M3 Fidelity Study 定量验证 |
| 提前行权/公司行动细节误差 | 少量场景失真 | 到期规则默认 + 偏差文档化；提前指派模型可选开关 |
| 范围蔓延（Stop Loss/Roll/多品种提前） | 交付延迟 | §1.3 排除清单即红线；SL/Roll 已排至 M2；M4 逐项立项 |

---

## 10. 决策记录（唯一事实来源）

| 决策 | 状态 | 结论 |
|---|---|---|
| D4 数据源 | ✅ 已确认 | 免费 + 合成数据起步；真实期权数据 M2 前再议（v0.7：移至 M3-A 前，重估标准 §16.2） |
| D2 交互形态 | ✅ 已确认 | Jupyter + CLI 起步；Streamlit M3 后按需 |
| 标的范围 | ✅ 已确认 | ETF（SPY/QQQ，美式实物结算）+ 指数（SPX，欧式现金结算） |
| D17 分红 | ✅ 已确认 | M0 用连续 q；`DividendModel` 抽象可替换；预留 discrete 接口；不假设标的共用同一模型 |
| D6 定价引擎 | ✅ 已确认 | CRR 进入 M0；`engine: auto`（SPX→BS、SPY→CRR），BS 可作 SPY 近似/回退；CRR 标准实现、不加高级 early-exercise 优化；报告标注模型类型 |
| §4.1 对象模型 | ✅ 已确认 | 关系与生命周期认可；M0 保持最小可用模型，不为未来需求预增字段 |
| §5 策略默认 | ✅ 已确认 | M0 四规则（DTE30 / delta0.20 / 止盈50% / DTE≤3 强退）；SL/Roll 移 M2 |
| D9 成交假设 | ✅ 已确认 | 收盘信号 → 次日开盘 mid + 5bps；信号/执行快照时点分离；禁止未来数据 |
| D14 样本外 | ✅ 已确认 | 70/30；train 搜索 / test 仅最终评估 / 禁止按 test 选参 |
| §6 指标 | ✅ 已确认 | IV Rank（252d）/ IVP / RV（20d）+ 四档分桶；Trade 保存 iv_rank_at_entry；指标清单封版 |
| §4.0 数据边界 | ✅ 已确认 | Raw/Derived 分离；IV/Greeks 由引擎计算；Provider 自带值仅作对照 |
| SPX 结算 | ✅ 已确认 | M0 统一按收盘价现金结算；明确为简化模型，不代表真实 SPX 全部 settlement convention |
| D11 保证金 | ✅ 已确认 | 命名 Simplified Reg-T-style Margin Model；不声称复刻券商 Reg-T；报告标注 research approximation |
| 性能目标 | ✅ 已确认 | benchmark target（10s / 5min），非 correctness requirement |
| D1/D3/D5/D7/D8/D10/D12/D13/D15/D16 | ✅ 已确认 | 与上述决策无冲突，按 Spec 通过 |
| §12 M0 验收 | ✅ 已确认 | 已写入 §12 并作为 M0 完成闸门 |
| 进入 M0 | ✅ 已授权 | M0 已完成（§12 九项验收全部通过；10 年 benchmark ≈8.3s < 10s 目标） |
| M0 演示脚本 | ✅ 已交付 | scripts/demo_m0.py：一键回测报告 + states/trades CSV；运行中发现并修复 CRR delta/gamma 捕获层级 bug（曾恒为 0），补 2 项回归测试 |
| M1-A 授权 | ✅ 已授权 | 用户拆分 M1-A/M1-B/M1-C；M1-A = 真实 SPY 日线 + 合成期权链 + 首条收益曲线；明确不依赖 yfinance 期权链、不做真实历史期权数据 |
| M1-A 数据获取 | ✅ 已确认 | yfinance 主路径 + 本地缓存 + 手动 CSV 兜底（三选一已确认） |
| M1-A 波动率输入 | ✅ 已确认 | 合成链 iv_atm = SPY 20 日滚动已实现波动率（开盘快照用 t−1 值，防前视） |
| M1-A 回测区间 | ✅ 已确认 | 默认 2005-01-01 ~ 2024-12-31（约 20 年） |
| M1-A 网络诊断 | ✅ 已记录 | Yahoo 直连被 429 限流（数据中心出口 IP）；Python 默认不走系统代理（本地代理软件监听 127.0.0.1）；设 HTTPS_PROXY/HTTP_PROXY 环境变量后 yfinance 正常 |
| M1-A 实现修正 | ✅ 已记录 | ① 开盘快照锚点只用开盘价与前收（原含当日收盘=前视）② 成交反解 IV 用快照自身 q（除息日深实值下界误报）③ implied_vol 下界加 1e-9 浮点容差（真实低波动行情的 erf 尾部饱和） |
| M1-B 范围重定义 | ✅ 已确认 | M1-B = 多策略架构（通用回测引擎）；仅 Sell Put + Buy & Hold 两种策略；暂不实现其他策略、不做 Sell Put 参数配置化；原 M1-B 内容（analysis 指标 / CLI 参数化 / sweep + train/test）推迟为 M1-C |
| M1-B 订单模型 | ✅ 已确认 | `OrderIntent.asset: OptionSpec \| EquitySpec`（EquitySpec = 标的 symbol）；OPEN = 买入股票/开期权，CLOSE = 卖出股票/平期权 |
| M1-B Buy & Hold 语义 | ✅ 已确认 | 首交易日开盘全仓买入（allocation 默认 100%，取整股）；期末最后交易日开盘清仓（引擎统一开盘成交）；单笔 Trade（exit_reason=hold_end）；**分红不付现**（与价格型基准口径一致，总回报版留待后续）；股票手续费 = 每单 + 每股 × 0（每股费率配置位已加，默认 0） |
| M1-B 期末清仓钩子 | ✅ 已确认 | `Strategy.on_final(ctx)`：最后交易日无条件调用（不受 margin_blocked 影响）；SellPutStrategy 用默认空实现（M0 行为不变） |
| M1-B Trade 泛化 | ✅ 已确认 | `Trade.spec: OptionSpec \| EquitySpec` + `asset_kind` 字段；期权专属字段（entry_iv/exit_iv/dte_at_entry/delta_at_entry）对股票为 None；trades.csv 增加 asset_kind/symbol 列（期权行数值不变） |
| M1-B 股票会计 | ✅ 已确认 | 组合新增 buy_equity + EquityLot（FIFO）；买入按成交价并入 avg_cost；卖出配对记 Trade；指派产生的无 lot 股票卖出仍不记 Trade（M0 行为不变）；states.positions 增加股票快照（mark=收盘价，unrealized=(S−avg_cost)×股数）；Greeks/归因仍仅期权域（股票 PnL 走净值曲线，扩展留待后续） |
| M1-B 股票保证金口径 | ✅ 已确认 | 买入时按 50% 初始保证金（CSP 模型为 0）检查可用资金并钳制股数；持仓日 margin_used 按 50% 市值计入（与 M0 口径一致）；100% 现金买入后 margin_blocked 属预期现象，期末清仓经 on_final 钩子不受影响 |
| M1-B 回归闸门 | ✅ 已确认 | Sell Put 全链路逐位不变（M1-A 离线复跑：期末 173,061.18 / 529 笔 / 最大回撤 −85.05%）；既有 80 项测试全绿 |
| M1-C 里程碑重排 | ✅ 已确认 | M1-C=基础研究能力（参数配置化/sweep/train-test/RV 分桶/基础风险指标/manifest+compare/基础研究报告）；M2=研究深度（MC/完整 regime/experiment tracking/SL-Roll/tail risk 深化）；M3=数据真实性（真实链/fidelity study）；§8 已重排 |
| M1-C 市场环境 | ✅ 已确认 | M1-C 仅 RV 四桶分桶（回答"低波动 vs 高波动环境表现是否不同"）；完整 bull/bear/sideways/高·低波动/极端事件分类属 M2，不在 M1-C 过度设计 |
| M1-C MDD 约束行为 | ✅ 已确认 | 无可行解必须**显式输出**；不得自动放宽 MDD、不得返回"最近似"冒充满足约束；可附当前参数空间最小 MDD 组合，但必须标注"最接近约束 ≠ 满足约束" |
| M1-C 合成链到期结构 | ✅ 已确认 | 保留月度到期、不加周度（保 M0/M1 回归）；DTE 分析用实际入场 `dte_at_entry`；报告声明到期结构限制；周度到期属 M3 |
| M1-C train/test | ✅ 已确认 | 固定 split：train 2005–2018 / test 2019–2024；sweep 仅用 train；test 仅最终 OOS evaluation；M1-C 不做 walk-forward（M2 再议）；报告声明 test 样本统计噪声 |
| M1-C 实验记录 | ✅ 已确认 | 轻量：manifest + experiment metadata + compare；完整 Experiment DB / GUI 属 M2 |
| M3-A 数据供应商 | ✅ 已确认（暂缓） | M1-C/M2 不做字段级设计；M3-A 立项前按 价格/API 可用性/历史覆盖/Greeks·IV/Bid·Ask/contract-level/License 重新评估；当前仅需 Historical Option Provider 抽象存在 |
| M2 MC 范围与 IV 建模 | ✅ 已确认（延后） | M2 立项时单独设计；区分"历史收益路径 bootstrap"与"期权 IV/vol dynamics 建模"两个问题；须明确模拟对象（underlying returns / volatility / option prices / strategy P&L 的组合）并逐项声明假设与局限 |
| M1-C 双基准 | ✅ 已确认 | Price Return 与 Total Return B&H（分红再投，§6.4）同时输出；定义写入报告；引擎与策略零改动；分红数据不足时显式降级声明而非静默简化 |
| M2 SL/Roll | ✅ 已确认 | M2 第一批；四臂对比（No SL/No Roll vs SL vs Roll vs SL+Roll）；作为研究假设验证，不预设"一定降低风险/提高收益" |
| M1-C sweep 保存粒度 | ✅ 已确认 | 默认 metrics-first（strategy/params/train-test 窗口/dataset metadata/code version/metrics/manifest）；`--save-all` 可选全量 states/trades/equity curves |
| M1-C 研究声明原则 | ✅ 已确认 | 参数搜索结果 ≠ 未来预测；"train 最优"只限"当前模型、数据与研究区间下、训练集内"；test 用于样本外验证（§17.2） |
| M1-C 模块边界 | ✅ 已确认 | 新增 `analysis` / `vol` / `research`；`backtest` / `report` 不单独立项（scripts 承担）；引擎核心在 M1-C 内 diff 为空（§15 AC-11） |

---

## 11. 决策问答记录（已闭环）

v0.4 全部待决策问题已答复并写入 §10 决策记录：CRR 进 M0 · 对象模型最小可用原则 · IV Rank/RV 默认值与分桶 · SPX 结算简化 · 保证金命名与声明 · 性能 benchmark 定性 · 其余推荐项整体通过。**当前无待决策项**，M0 已获授权开始。

**v0.6（M1-B）**：用户直接下达 M1-B 范围（多策略架构；仅 Sell Put + Buy & Hold；暂不做参数配置化与其他策略）；上述 §10 中 M1-B 各行决策均依此授权记录，无待确认项。

**v0.7（M1-C）**：用户下达 M1-C 十二项决策（里程碑重排 / RV 分桶 / MDD 无可行解行为 / 月度到期保留 / 固定 train-test / 轻量实验记录 / M3-A 供应商暂缓 / MC-IV 建模延后 / 双基准 / SL-Roll 首批 / metrics-first / 无预测原则），已全部写入 §10 决策记录与 §15 验收标准；**当前无待确认项**。M2 立项时需单独确认：Monte Carlo 模拟对象与 IV 建模、walk-forward 范围、Experiment Tracking 形态。

---

## 12. M0 Acceptance Criteria（✅ 已确认，M0 完成闸门）

M0 完成后，以下每一条都必须有对应测试或脚本证据并通过（性能项除外，性能是 benchmark target）：

| # | 验收项 | 通过条件 |
|---|---|---|
| 1 | BS / CRR 与已知理论结果对拍 | Hull 教材算例一致（容差 1e-6）；put-call parity 逐点成立；CRR 步数 50→200→800 单调收敛至 BS；CRR 美式 Put ≥ 欧式 Put；r=0 且 q=0 时美式 Put == 欧式 Put；美式 Call（q=0）== 欧式 Call |
| 2 | Greeks 基本 sanity | 解析 Greeks 与有限差分一致（相对 1e-3）；符号与边界：0≤\|Δ\|≤1、Γ≥0、Vega≥0、多头 Θ<0（q=0）；parity 派生关系 Δc − Δp = e^(−qT) |
| 3 | 防前视 | Close(t−1) 信号 → Open(t) 执行：篡改 Open(t) 快照不影响 OrderIntent 集合；数据平移测试决策集不变 |
| 4 | Portfolio / Cash / Position / PnL 正确性 | 资金守恒不变量（equity 逐日衔接）；FIFO 配对；realized + unrealized 与现金账一致；手算场景（开仓权利金、平仓损益）逐分一致 |
| 5 | Expiration / Assignment 生命周期 | OTM 到期权利金全收；ITM 实物指派（按行权价接货 → 次日开盘卖出）与现金结算（现金轧差）金额正确；Trade 归档 exit_reason 正确（expire / assign / close） |
| 6 | Sell Put 四默认规则 | DTE≈30 选期、delta≈0.20 选价、止盈 50%、DTE≤3 强制退出、优先级（DTE≤3 > 止盈 > 开仓）、weekly 频率、max_open_positions、sizing 向下取整 |
| 7 | MC 固定 seed 可复现 | 同 seed 两次路径生成逐位一致；不同 seed 结果不同 |
| 8 | Raw → Derived 可重算 | 由 Raw mid 反解 IV 再定价回 mid（1e-6）；Greeks 自 Raw 重算与缓存一致 |
| 9 | 基础性能 benchmark | `scripts/bench_m0.py`：10 年日频回测（合成数据）报告耗时，目标 <10s（benchmark target，非测试门槛） |

**M0 交付物清单**：见 §8 里程碑表 M0 行。

**验收状态（✅ 全部通过）**：80 项功能测试 + 1 项 benchmark 标记测试全绿；10 年合成回测 benchmark ≈8.0s（目标 <10s）；优化全程保持数值结果逐位一致（10 年终值 44,640.09 不变）。

---

## 13. M1-A Acceptance Criteria（真实 SPY 日线接入）

| # | 验收项 | 状态 |
|---|---|---|
| 1 | 真实 SPY 日线全链路 | 2005–2024 首跑成功：5033 个交易日、529 笔交易、16.1s；数据 5081 个交易日（含 70 天缓冲），来源 yfinance，落缓存 `data/spy_daily.csv` |
| 2 | 第一条完整收益曲线 | `experiments/m1a/equity_curve.png`（策略净值 vs 买并持有 + 回撤副图）+ states/trades/prices.csv + summary.txt ✅ |
| 3 | 防前视保持 | 开盘快照仅用开盘价与前收（波动率/股息率取 t−1）；"抹掉未来收盘"对照实验断言开盘快照逐位一致（`test_open_snapshot_no_lookahead`）✅ |
| 4 | 滚动已实现波动率 | 20 日滚动 RV（年化）驱动合成链 iv_atm；与手算 std(ddof=1)×√252 逐位对拍 ✅ |
| 5 | 股息率真实化 | 过去 12 个月实际分红 ÷ 前收（截至前收口径）；测试对拍；CSV 兜底无分红时 q=0 ✅ |
| 6 | 缓存离线可复现 | 缓存命中后完全离线；同缓存两次运行数据逐位一致（测试断言 cache 路径）✅ |
| 7 | 网络失败可操作提示 | 三级获取全部失败时给出明确报错：代理 setx 命令 + 手动 CSV 路径与列格式 ✅ |
| 8 | 既有回归不回退 | 80 功能测试全绿；10 年合成 benchmark 7.97s < 10s；终值 44,640.09 不变 ✅ |

**首跑数字（研究近似，期权报价为合成）**：期末净值 173,061（+73.1%），买并持有 SPY +387.2%；最大回撤 **−85.0%**（2008-10-10，无止损裸空 Put 的尾部风险画像）；胜率 93.2%，离场：止盈 488 / DTE≤3 强退 41。

---

## 14. M1-B Acceptance Criteria（多策略架构：Sell Put + Buy & Hold）

M1-B 完成后，以下每一条都必须有对应测试或脚本证据并通过：

| # | 验收项 | 通过条件 |
|---|---|---|
| 1 | 通用订单与成交 | OrderIntent 支持 OptionSpec 与 EquitySpec；股票 OPEN/CLOSE 全链路成交（滑点/手续费/保证金钳制）；Sell Put 路径行为不变 |
| 2 | Buy & Hold 生命周期 | 首交易日开盘买入、最后交易日开盘清仓、中途零订单；单笔 Trade（asset_kind=equity，exit_reason=hold_end，qty=股数） |
| 3 | Buy & Hold 会计守恒 | 清仓后 equity = start + Σpnl 逐位相等；持仓日 states.positions 含股票快照（mark/unrealized 正确）；margin_used = 50% 市值口径 |
| 4 | 防前视 | B&H 股数基于 t−1 收盘定价、开盘成交价钳制（含滑点/手续费）；篡改开盘价不改变策略意图 |
| 5 | 期末清仓与保证金解耦 | 100% 现金买入后 margin_blocked 不阻止期末清仓（on_final 钩子）；reject/liquidate 政策下 B&H 均能正常清仓 |
| 6 | 配置与工厂 | type/params 不匹配时校验拒绝；build_strategy 正确分发；CLI `--strategy {sell_put,buy_hold}` |
| 7 | 回归闸门 | 既有 80 项测试全绿；M1-A 离线复跑逐位一致（期末 173,061.18 / 529 笔 / 最大回撤 −85.05%）；10 年合成 benchmark 终值 44,640.09 不变 |
| 8 | 交付体验 | trades.csv 兼容扩展（asset_kind/symbol，期权行数值不变）；summary.txt 记录 strategy；可视化报告兼容两种策略（时间线/直方图正确渲染股票交易） |

**M1-B 明确不做**：其他策略（Covered Call / Put Spread / Wheel，M4 沿本架构立项）；Sell Put 参数配置化；analysis 指标（IV Rank/IVP/RV + 分桶）；sweep + train/test；股票分红付现；股票腿 Greeks/归因。

---

## 15. M1-C Acceptance Criteria（基础研究能力，v0.7）

> v0.7 经用户 12 项决策确认（§10/§11）。实施时以下每一条都必须有对应测试或脚本证据。
> **核心原则**：研究层（`analysis` / `vol` / `research`）全部位于引擎之外，只重复调用引擎；引擎核心模块在本里程碑内 **diff 必须为空**（AC-11）。

### 15.1 目标与边界

回答第一阶段核心研究问题：**Sell Put 的 DTE / Delta / Take Profit 如何影响收益与风险**（Return / CAGR / MDD / risk-adjusted / 胜率 / 尾部）。

- **做**：参数配置化、网格 sweep、2D 热图分析、固定 train/test、RV 市场环境分桶、基础风险指标、manifest + compare、初步研究报告、双基准。
- **不做**：Monte Carlo、完整 market regime 分类、walk-forward、SL/Roll、真实期权数据、周度到期、实验数据库/GUI —— 分别属 M2 / M3（§16）。

### 15.2 功能需求

| ID | 需求 |
|---|---|
| F1 | 策略参数配置化：`run_backtest.py` 增加 `--dte --delta --tp`（可选 `--dte-exit --entry-frequency --max-open-positions`）；Pydantic 校验沿用（delta∈(0,0.5)、tp∈[0,1]） |
| F2 | 网格 sweep：`run_sweep.py --dte 20,30,45,60 --delta 0.10,...,0.30 --tp 0.25,...,1.00` 全组合（≤80）；多进程（Windows spawn）；每组合独立 config/seed，结果与网格大小、顺序、进程数无关 |
| F3 | 2D 参数分析：每个指标输出 DTE×Delta 热图（固定 TP）+ 1D 边际线 + 明细表；DTE 维度按**实际入场 `dte_at_entry`** 分桶解释；报告声明合成链月度到期限制（§5.1 v0.7） |
| F4 | 约束筛选视图：`--max-mdd 0.30 --sort-by cagr` 只在 train 段筛选排序；**可行集为空时显式输出"无可行解"**，不自动放宽、不返回最近似冒充；可附最小 MDD 组合并标注"最接近约束 ≠ 满足约束" |
| F5 | 固定 train/test：`split.train_end=2018-12-31`（train 2005–2018 / test 2019–2024）；sweep 只能读 train；test 仅最终 OOS evaluation；不做 walk-forward；报告声明 test 样本统计噪声 |
| F6 | 基础风险指标（`analysis`）：总收益/CAGR/年化波动率/Sharpe(rf 可配)/Sortino/Calmar/MDD+回撤持续天数/胜率/平均盈亏/Profit Factor/年均交易数/P5·最差单笔/收益分布/资本效率 |
| F7 | RV 市场环境分桶（`vol`）：入场日 RV（20d 滚动、252d 窗口分位）四桶 <25/25–50/50–75/≥75；每桶 笔数/胜率/平均 PnL —— 只回答"低波动 vs 高波动环境表现是否不同" |
| F8 | Manifest：每 run 写 `manifest.json`（完整展开配置、git commit、包版本、数据集指纹、train/test 窗口、`test_eval_count`、指标）；`compare_runs.py` 合并对比（train 可排序、test 禁排序，D14） |
| F9 | 基础研究报告：自动生成 Markdown —— 假设清单（§17.1）、窗口/网格、约束可行性、Top-N、敏感性观察、效度声明（§17.3 模板） |
| F10 | 双基准：Price Return 与 Total Return B&H 同时输出（§6.4）；超额收益对双基准分别报告 |
| F11 | 无预测原则：报告措辞必须是"在当前模型/数据/研究区间下，train 上 X 表现最好"；禁止把参数搜索结果表述为未来预测（§17.2） |

### 15.3 非功能需求

| ID | 需求 |
|---|---|
| N1 | 性能：80 组合 × 20 年离线 sweep ≤ 15 分钟（8 进程；benchmark target 非闸门） |
| N2 | 确定性：同组合结果与网格大小/顺序/进程数无关；sweep 连跑两遍指标 CSV 逐字节一致（manifest 时间戳字段除外） |
| N3 | 防前视：研究层复用引擎，继承快照纪律；新增结构性测试——篡改 test 段数据不改变参数选择（AC-5） |
| N4 | 保存粒度：默认 metrics-first（strategy/params/train-test 窗口/dataset metadata/code version/metrics/manifest）；`--save-all` 可选全量 states/trades/equity curves（防存储膨胀与 I/O 开销） |
| N5 | 文档同步：Spec 为唯一事实来源；README 相应更新 |

### 15.4 CLI / API 变更

```text
run_backtest.py  + --dte --delta --tp [--dte-exit --entry-frequency --max-open-positions]
run_sweep.py     新增：--dte/--delta/--tp（逗号列表）、--train-end、--max-mdd、--sort-by、
                  --save-all、--out runs/...
compare_runs.py  新增：--runs A B C
config.py        + sweep: {dte_target:[...], delta_target:[...], profit_target_pct:[...]}、
                  split: {train_end}、benchmark: {price_return, total_return}
新模块            analysis.py / vol.py / research.py
```

### 15.5 数据模型（新增类型）

```text
Metrics            { total_return, cagr, ann_vol, sharpe, sortino, calmar, max_dd,
                     dd_duration_days, win_rate, profit_factor, avg_win, avg_loss,
                     n_trades, trades_per_year, p5_trade_pnl, worst_trade_pnl,
                     benchmark_price_return, benchmark_total_return,
                     excess_vs_price, excess_vs_total, capital_efficiency }
SegmentMetrics     { train: Metrics, test: Metrics, full: Metrics }
SweepResult        { params, segment: SegmentMetrics, manifest_path }
ExperimentManifest { config(展开), git_commit, package_version, dataset_fingerprint,
                     train_end, test_eval_count, metrics, created_at }
```

### 15.6 验收标准

| # | 验收项 | 通过条件 |
|---|---|---|
| 1 | 网格运行 | DTE=[20,30,45] × Delta=[0.1,0.2,0.3]（TP 固定）→ 9 个独立回测，产出统一格式 Return/CAGR/MDD/胜率明细表（9 行 + 基准行） |
| 2 | 全维度网格 | DTE=[20,30,45,60] × Delta=[0.10,0.15,0.20,0.25,0.30] × TP=[0.25,0.50,0.75,1.00] 共 80 组合离线跑完；指标 CSV 每组合一行 |
| 3 | 指标金标准 | 默认组合（30/0.20/0.50，2005–2024）经 `analysis` 重算 == 现有手算值：期末 173,061.18 / total_return 0.730612 / MDD −0.850484 / 529 笔 / 胜率 93.2% |
| 4 | 指标手算锚点 | CAGR/Sharpe/Sortino/Calmar/Profit Factor 用固定小序列手算期望值写死测试 |
| 5 | train/test 隔离 | train_end=2018-12-31 下，篡改 2019 年后测试段数据**不改变所选参数组合**（结构性测试）；test 指标仅最终报告；`test_eval_count` 写入 manifest |
| 6 | 约束筛选 | `--max-mdd 0.30`：输出表仅含 train MDD ≥ −30% 的组合并按 CAGR 降序；可行集为空时显式输出"无可行解"（不自动放宽、不取最近似）；附最小 MDD 组合并标注"最接近约束 ≠ 满足约束" |
| 7 | 复现性 | 同 dataset+config+seed 连跑两遍指标 CSV 逐字节一致；组合结果与进程数/网格顺序无关 |
| 8 | Manifest + Compare | manifest.json 字段完整（§15.5）；compare 对 3 个 run 输出合并对比表；test 段禁排序 |
| 9 | RV 分桶 | 每笔 Trade 按入场日 RV 分位落入 4 桶；分桶表含笔数/胜率/平均 PnL；桶边界与手算分位一致 |
| 10 | 热图报告 | 离线 HTML：DTE×Delta 热图（≥4 指标）+ 约束筛选表 + 假设声明 + 效度声明 + **合成链月度到期限制声明** + 实际入场 DTE 分桶 |
| 11 | 引擎零改动 | sim/strategy/portfolio/pricing/execution/margin/instruments/dividend/market_data/data/mc 与 M1-B 版本 **diff 为空** |
| 12 | 回归闸门 | 既有 93 项测试全绿；10 年合成 benchmark 终值 44,640.09 不变；M1-A 离线复跑 173,061.18 不变 |
| 13 | 性能 | 80 组合 20 年 sweep ≤ 15 分钟（8 进程；benchmark target） |
| 14 | 研究报告 | 报告含：假设清单（§17.1）、train/test 窗口、约束可行性、Top-N、敏感性观察、§17.3 效度声明、test 统计噪声声明 |
| 15 | 双基准 | Price/Total Return 基准同时输出；Total Return 指数与手算再投复现一致；引擎与 B&H 策略零改动 |

### 15.7 实施顺序（建议）

1. `analysis` 指标模块 + 金标准测试（AC-3/4）
2. `vol` 模块（IV Rank / RV / RV 分桶）+ 测试（AC-9）
3. config 增加 sweep/split/benchmark 字段 + `run_backtest.py` 参数 flag（F1）
4. `research`：单组合运行封装 + train/test 分段指标（AC-5 结构性测试）
5. manifest 写入 + `compare_runs.py`（AC-8）
6. `run_sweep.py` 多进程网格 + metrics-first 聚合（AC-1/2/7/13）
7. 约束筛选视图 + 无可行解行为（AC-6）
8. 热图报告 + 研究 Markdown 报告（AC-10/14/15）
9. 官方研究跑通：DTE×Delta×TP 全网格（train 搜索）→ 约束筛选 → test 最终验证
10. Spec/README 修订 + 全量回归验收（AC-11/12）

### 15.8 交付与验收状态（✅ 全部通过）

**交付物**：`analysis.py`（指标 + 双基准）、`vol.py`（IV Rank / RV / RV 四桶）、
`research.py`（单次运行 / train-test 切分 / sweep / manifest / 约束筛选 / 加载器）；
`scripts/`：`run_sweep.py`、`finalize_experiment.py`、`build_sweep_report.py`、`compare_runs.py`；
`run_backtest.py` 增加策略参数 flag；测试新增 `test_analysis/test_vol/test_research/test_manifest/
test_sweep/test_selection/test_config/test_benchmark` 与 4 个 CLI 测试文件。

**验收证据（2026-09-13）**：

| 项 | 证据 |
|---|---|
| AC-1 网格运行 | DTE=[20,30,45] × δ=[0.10,0.20,0.30]（TP 固定）9 组合 + 基准行，`sweep_metrics.csv` 逐组一行 |
| AC-2 全维度网格 | 4×5×4=**80 组合**全部跑完（`runs/sweep-official`，每组合 manifest + 指标 CSV） |
| AC-3/4 指标金标准与手算 | M1-A 重算 == 173,061.18 / 0.730612 / −0.850484 / 529 笔 / 胜率 93.2%；CAGR/Sharpe/Sortino/Calmar/PF 手算锚点 |
| AC-5 train/test 隔离 | 篡改 test 段数据 → train 段指标**逐位不变**、按 train 选出的参数不变（合成矩阵 + 真实数据双重验证）；搜索模式不计算 test |
| AC-6 约束筛选 | 官方 sweep：80 组合 **0 个**满足 MDD ≤ 30% → 显式输出「无可行解」+「最接近约束 ≠ 满足约束」；不自动放宽 |
| AC-7 复现性 | 同配置连跑两遍指标 CSV 逐字节一致；结果与进程数（1 vs 2/8）、网格顺序无关 |
| AC-8 manifest/compare | manifest 含 §15.5 全部字段（+ 数据指纹/git/dirty/python）；`compare_runs.py` 三实验合并表，test 段禁排序 |
| AC-9 RV 四桶 | 每笔按入场决策时点已知的 RV 分位归桶（四桶 + 边界手算一致） |
| AC-10/14 报告 | `report.html`（离线自包含）+ `research_report.md`：6 指标热图 + 1D 边际、约束筛选、Top-N、敏感性观察、假设登记表（§17.1）、效度声明（§17.3）、**月度到期声明**、**实际入场 DTE 分桶**、复现命令 |
| AC-11 引擎零改动 | 11 个核心模块 + `run_backtest.py` 的 `git diff` 为空 |
| AC-12 回归闸门 | 213 项测试全绿；M1-A `173,061.18 / 0.730612 / −0.850484 / 529`、M1-B `484,689.27 / 3.846893 / −0.564619 / 1`、bench_m0 终值 `44,640.09` 均逐位一致 |
| AC-13 性能 | 80 组合 × 20 年离线 sweep **366.4s**（8 进程，4.6s/组合）≤ 15 分钟目标 |
| AC-15 双基准 | `total_return_index` 手算复现（+15.5% vs 价格型 +10%）；分段复合一致；引擎与 B&H 策略零改动；降级显式声明 |

**官方研究结论（在当前假设下，详见 `runs/sweep-official/research_report.md`）**：该参数网格内
**不存在**满足 MDD ≤ 30% 的参数（最小回撤 −62.95%）；delta 是主导变量（平均 CAGR −3.59% → +3.80%）；
train 最优组合（DTE 30 / δ0.30 / TP 50%）在 test 段 +58.70%，但同期落后价格型基准 75.6 个百分点、
落后总回报型基准 99.2 个百分点——**DTE/Delta/TP 三个维度不足以改变尾部风险结构**。

**一处待决策的方法论问题**（记录在案，不阻塞交付）：当 MDD 约束**无可行解**时，Spec §15 F4/AC-6
只规定"必须显式输出不可行、不得放宽、不得用最接近冒充"，**未规定后续动作**。当前实现采取
"报告不可行性 + 对 train 最优组合做**唯一一次**样本外验证（报告标注这不是风险可行选择）"。
是否改为"仅报告不可行性、完全不做样本外评估"，留待 M2 立项前确认。

---

## 16. M2 / M3 高层范围（逐项单独立项）

### 16.1 M2 —— 研究深度

- **Monte Carlo**（v0.7 决策：立项时单独设计）：
  - 目标：回答"历史上未发生（或仅一次）的坏情景下策略会怎样"——未来 1 年收益分布、MDD 分布（P(MDD>30%/50%/80%)）、接近归零概率、跨参数风险对比（如 Delta 0.15 vs 0.30）。
  - 方法候选：历史收益 **block bootstrap**（主；保留实证厚尾/波动聚集）、**参数化厚尾模型**（辅；外推超出历史）、GBM 仅作 sanity 基准（附录 C.4 锚点保留）。
  - **立项时必须明确模拟对象**：underlying returns / volatility dynamics / option prices / strategy P&L 中的哪几项组合，并逐项声明假设与局限；其中"期权 IV 如何随模拟路径演化"是 MC 可信度上限，单独设计。
- **完整 Market Regime Analysis**：bull/bear/sideways（趋势类）、high/low vol（波动率类）、极端事件（2008/2020/2022 案例标注）；按 regime 输出 CAGR/MDD/胜率/收益分布/持仓期/尾部亏损/回撤时长；regime × 参数交互分析。
- **Experiment Tracking 完整化**：manifest 索引库（SQLite/DuckDB）+ 实验对比/查询界面（Notebook 或 Streamlit 薄壳，D2 精神）。
- **SL / Roll（第一批）**：四臂对比 —— No SL/No Roll vs SL vs Roll vs SL+Roll；作为**研究假设**验证（不预设"SL/Roll 一定降低风险或提高收益"）；优先级 止损>止盈>roll>DTE>开仓（§5 已预留 `OrderReason.STOP_LOSS/ROLL`）。
- **Tail Risk 深化**：收益分布、MDD 分布、ruin 概率、极端亏损事件案例分析。
- （可选）**Walk-forward / rolling evaluation**：缓解单一 test 窗口统计功效问题（v0.7 决策：M1-C 不做）。

### 16.2 M3 —— 数据真实性

- **Real Historical Option Chain**：
  - M3-A 立项前**重新评估供应商**（数据价格 / API 可用性 / 历史覆盖范围 / Greeks·IV / Bid·Ask / contract-level / License），M1-C/M2 不做字段级设计（v0.7 决策）；当前仅要求 Provider 抽象存在。
  - 接入方式：`HistoricalOptionProvider(MarketDataProvider)` 实现既有接口（sessions / close_snapshot / open_snapshot）→ 引擎与策略**零改动**；`MarketSnapshot` 契约（bid/ask/last/volume/OI）已具备（§4.0）。
  - 数据质量策略：QuotePolicy（最小 OI/成交量、最大价差、last 陈旧度）在 Provider 层过滤；策略选价天然自适应（只遍历有报价的行权价）。
  - DataCache 落地（DuckDB/Parquet，D5 决策兑现）；manifest 增加 `data_version`。
  - 周度到期属本阶段数据真实性议题（v0.7 决策：M1-C 合成链保持月度）。
- **执行/保证金真实化**：Bid/Ask 成交口径、流动性过滤、Early Assignment 概率模型（配置开关，默认关）、更真实保证金（Portfolio Margin 仅在确有必要时立项）。
- **Synthetic vs Real Fidelity Study**：同配置同窗口同纪律跑 synthetic 链 vs 真实链，对比 CAGR/MDD/权利金统计/入场 IV 分布——直接回答"模型真实性是否改变研究结论（synthetic 是否系统性高估收益/低估风险）"。

---

## 17. 金融假设登记与研究效度（v0.7）

### 17.1 金融假设登记表（写入每个实验报告）

| # | 假设 | 位置 | 标注 |
|---|---|---|---|
| 1 | 标的 = 真实 SPY 未复权日线（yfinance/缓存/CSV 三级） | `data.py` | realistic |
| 2 | 期权报价 = 合成链（BS 定价） | `market_data.py` | synthetic |
| 3 | 合成链 iv_atm = 20d 滚动已实现波动率（t−1 口径） | `HybridProvider` | synthetic；**无波动率风险溢价（VRP）** |
| 4 | 合成链 skew=0（默认）、价差 5bps 常数；**月度到期结构** | `SyntheticChainEngine` | synthetic |
| 5 | SPY 定价 = CRR 200 步美式；BS 仅近似（报告标注） | `pricing.py` | simplified（已声明） |
| 6 | IV 反解 = BS 口径（美式标的亦然，市场惯例近似） | `implied_vol` | simplified |
| 7 | 分红 = 连续 q（过去 12 个月实际分红 ÷ 前收） | `PerDayDividendModel` | simplified |
| 8 | 成交 = Open(t) mid ± 5bps + $0.65/合约 + $1/单 | `FillModel` | simplified |
| 9 | 保证金 = Simplified Reg-T-style（ETF 20%） | `margin.py` | simplified（research approximation，已声明） |
| 10 | 指派 = 仅到期规则（ITM 收盘指派），无提前指派 | `sim.py` | simplified |
| 11 | 指派后次日开盘卖出 | `sim.py` | simplified |
| 12 | 无风险利率恒定 4%；现金不计息 | `config.py` | simplified |
| 13 | 仓位 = 可用资金 50% ÷ 单合约保证金，≤1 腿 | `strategy._size` | strategy/research choice |
| 14 | 无 Stop Loss / Roll（M1 范围） | `strategy.py` | strategy choice；**−85% MDD 的直接成因之一（研究假设，M2 验证，不预设结论）** |
| 15 | 基准 = 价格型 + 总回报型 B&H（§6.4） | `analysis`（M1-C） | simplified 口径（约定写入报告） |
| 16 | 入场 delta 按成交时快照记录（与目标值偏差属正常） | `sim.py` | engineering choice |

### 17.2 研究效度（报告强制声明）

- **可以比较可信地研究**：策略规则机制与生命周期（金标准背书）；参数敏感性（在 synthetic 假设**内部**的方向性结论）；标的下跌→亏损→指派传导机制；工程正确性（无前视/守恒/复现）。
- **不能声称**：任何绝对收益水平或"真实可交易业绩"；权利金水平与真实市场可比（无 VRP）；尾部风险的真实量级（无跳变、IV=RV 滞后、无极端 IV 飙升定价）。
- **Synthetic 链的影响**：① RV 平均低于市场 IV → 合成权利金系统性偏低 → 模拟收益偏低；② 崩盘期用滞后 RV 定价 → 尾部亏损方向近似、幅度不可信；③ 进出场时机由 RV 变化驱动而非市场 IV 变化 → 交易行为分布与真实回测不同。
- **无预测原则（v0.7 决策）**：参数搜索结果（如"train 上 Delta=0.20 最好"）只能表述为"在当前模型、数据与研究区间下，训练集内表现最好"；**不得**包装为"未来最优参数/预测未来表现"。Test 的唯一作用是验证该结论能否样本外成立；test 指标存在统计噪声（样本量有限、尾部事件少），必须声明。
- **Train/Test 纪律**：train 可用于参数搜索；test 仅最终评估；禁止按 test 结果回头选参（sweep 对比视图 test 段禁排序，D14；`test_eval_count` 写入 manifest）。

### 17.3 报告效度声明模板（自动内嵌）

1. 本报告结论仅在 §17.1 假设与 synthetic 期权报价下成立；
2. 期权报价为合成（波动率 = SPY 20d 已实现波动率），非真实历史期权价格；收益与风险数字为研究近似，不构成任何未来表现预测；
3. 合成链为月度到期结构（M1-C 决策），DTE 分析按实际入场 DTE 分桶；
4. 参数在 train（2005–2018）上选择，test（2019–2024）仅作样本外验证；test 样本有限，指标含统计噪声；
5. 参数搜索结果不是未来预测（§17.2）。

---

## 附录 A：保证金公式

### A.1 Simplified Reg-T-style（空头 Put 初始保证金，每股口径）—— research approximation

```
Requirement = max( 0.20 × S − OTM + Premium, 0.10 × K + Premium )
OTM = max(S − K, 0)        # 空头 Put 的价外金额（S > K 时为正）；v0.4 修正原 v0.3 的 K−S 笔误
```

- 宽基指数期权（SPX 等）：系数 0.20 换为 0.15（不同券商略有差异，做成配置项并注明出处）。
- 合约保证金 = Requirement × multiplier（默认 100）。
- 空头收取的权利金计入可用资金。
- **声明（✅ 已确认）**：本模型为研究用简化近似（ETF 20% / 宽基指数 15%），**不是对任何券商 Reg-T / broker-specific margin 的完整复现**；代码 docstring 与报告必须标注 "research approximation"。

### A.2 Cash-Secured Put（CSP）

```
Requirement = max(K − Premium, 0) × multiplier      # 即履约全额 − 权利金
```

### A.3 保证金政策

- `reject`：可用资金 < 新仓保证金需求 ⇒ 本轮不开仓（默认）。
- `liquidate`：持仓保证金需求 > 可用资金 ⇒ 挂次日开盘强平单（按亏损最大优先）。

---

## 附录 B：PnL 归因（逐日、每腿近似分解）

对单个期权腿（数量 n，ΔS 为当日标的变动，Δt 为日历年分数）：

```
ΔPnL ≈ n × [ Δ·ΔS + ½·Γ·(ΔS)² + Θ·Δt + Vega·Δσ + Rho·Δr ] + Residual
```

- Δσ = 该腿当日 IV 变动（回测用市场 IV 序列；MC 用曲面模型）。
- `Residual` = 离散化、曲面位移等未建模项，**必须在归因表中显式报告**（作为模型质量监控指标）。
- 股票腿 PnL = qty × ΔS；现金利息按 `interest_on_cash` 计。

---

## 附录 C：定价公式与验证锚点

### C.1 Black-Scholes（欧式，连续分红 q）

```
d1 = [ln(S/K) + (r − q + σ²/2)T] / (σ√T)，  d2 = d1 − σ√T
C  = S·e^(−qT)·N(d1) − K·e^(−rT)·N(d2)
P  = K·e^(−rT)·N(−d2) − S·e^(−qT)·N(−d1)

Δc = e^(−qT)·N(d1)；  Δp = e^(−qT)·(N(d1) − 1)
Γ  = e^(−qT)·φ(d1) / (S·σ·√T)
Vega = S·e^(−qT)·φ(d1)·√T
Θc = −S·e^(−qT)·φ(d1)·σ/(2√T) − r·K·e^(−rT)·N(d2) + q·S·e^(−qT)·N(d1)
Θp = −S·e^(−qT)·φ(d1)·σ/(2√T) + r·K·e^(−rT)·N(−d2) − q·S·e^(−qT)·N(−d1)
ρc = K·T·e^(−rT)·N(d2)；  ρp = −K·T·e^(−rT)·N(−d2)
```

### C.2 CRR 二叉树（美式）

```
u = e^(σ√dt)， d = 1/u， p = (e^((r−q)dt) − d) / (u − d)
V = max( intrinsic, e^(−r·dt)·(p·Vu + (1−p)·Vd) )     # 美式回代取 max
```

### C.3 验证锚点

- Put-call parity：`C − P = S·e^(−qT) − K·e^(−rT)`（逐点断言）。
- Hull《Options, Futures, and Other Derivatives》标准算例做固定点对拍。
- CRR 步数 50→200→800 相对 BS 收敛误差单调下降。
- IV 往返：`|implied_vol(BS(S,K,T,r,q,σ)) − σ| < 1e-6`。

### C.4 MC 解析 sanity check（真实测度 GBM）

GBM 真实测度（漂移 μ）下，到期 Put 期望赔付有解析式（BS 公式中 r→μ）：

```
E_real[max(K − S_T, 0)] = K·N(−d2') − S0·e^(μT)·N(−d1')，
d1' = [ln(S0/K) + (μ + σ²/2)T]/(σ√T)， d2' = d1' − σ√T
```

⇒ CSP 持有到期的期望 PnL = `Premium − E_real[payoff]`（现金账户不计息），用于 MC 均值校验（95% CI 覆盖）。

---

*（本文档 v0.7.0 —— M0 / M1-A / M1-B / M1-C 均已完成。§10 决策记录为唯一事实来源，§12/§13/§14/§15 为 M0/M1-A/M1-B/M1-C 完成闸门（均已通过）。后续里程碑（M2 研究深度 / M3 数据真实性）见 §16。）*
