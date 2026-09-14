"""sellput — Options Research Platform（M0 垂直切片 + M1-A 真实数据接入 + M1-B 多策略）。

模块（Spec §2.3）：
- instruments  期权合约与股票规格、交易日历
- dividend     分红模型抽象（连续 q / 按日查询）
- pricing      定价引擎（BS / CRR）+ Greeks + IV 求解
- config       类型化配置（Pydantic v2）
- market_data  Raw 数据契约 + SyntheticProvider（M0）+ HybridProvider（M1-A）
- data         真实标的价格加载（缓存 → yfinance → 手动 CSV，M1-A）
- margin       保证金模型（Simplified Reg-T-style / CSP）
- portfolio    账户组合与逐日会计（期权 + 股票，FIFO）
- execution    下单与成交（期权 + 股票订单）
- strategy     策略层（Sell Put + Buy & Hold，Strategy ABC）
- sim          日频事件引擎（多策略）
- analysis     回测指标（Spec §6 / §15.5；纯函数，M1-C）
- vol          波动率统计（IV Rank / RV / RV 四桶，Spec §6.2 / §15 F7；M1-C）
- research     研究层（单组合运行 + train/test 分段指标，Spec §15 F5；M1-C）
- mc           Monte Carlo 路径生成器（完整引擎 M2）
"""

__version__ = "0.3.0"
