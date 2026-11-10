# Negative Funding Strategy · 策略与执行交接

当前交接基线：**RC2 `nfb.strategy.20261108.rc2`，JSON 协议 2.1.0**。这是已冻结、可离线复跑的策略侧参考版本；生产准入与真实成交仍待执行方验收，`submission_allowed=false`。

我们负责策略、提前信号、风控、JSON 决策和真实回执分析。执行方负责下单、拆单、撤单、补腿和资金划转。信号模型独立放在 [negative-funding-signals](https://github.com/leslieshen1/negative-funding-signals)。

最近检查：[2026-11-10 mock 与历史回归报告](docs/MOCK_REVIEW_20261110.md)。32条合成路径中26条完整退出、6条保留未完成义务；未获得实盘准入。

13.19%与5.86%属于不同日期/起始状态/退出与年化口径，现已逐项对齐：[差异桥接报告](docs/RETURN_RECONCILIATION_20261110.md)。

## 从这里开始

| 顺序 | 内容 |
|---|---|
| 1 | [策略规则与成本边界](docs/STRATEGY.md) |
| 2 | [执行方交接与输入输出](docs/HANDOFF.md) |
| 3 | [固定输入和预期输出](runtime/runtime_examples/) |
| 4 | [E01—E14 联调验收](runtime/联调验收.md) |
| 5 | [当前状态与尚缺证据](docs/STATUS.md) |
| 6 | [历史研究复跑说明](docs/REPRODUCIBILITY.md) |

## 三条命令跑通参考计算

只需 Python 3.9+ 标准库；示例是历史行情和合成账户，不接交易所。

```sh
python3 -S runtime/verify_package.py
python3 -S runtime/decision.py --input runtime/runtime_examples/01_historical_market_enter.input.json --replay
python3 -B scripts/verify_release.py
```

离线测试：

```sh
python3 -m pip install -r requirements-dev.txt
python3 -B -m unittest discover -s runtime/tests -v
python3 -B -m unittest discover -s support/tests -v
```

正常账户输入去掉 `--replay`，按实际接收时钟拒绝过期请求。目标量、许可切片、转账意图和真实成交分开。

## 目录

- `runtime/`：原字节冻结的 RC2 决策、模型、三个 Schema、校验器及样例。
- `support/`：资金风控、账本、回执对账、成本分析、执行方验收模板；补充协议独立版本。
- `docs/`：阅读顺序、责任、字段和验收边界。
- `frozen_reference/`、`research/`：可追溯的研究源码；历史结果不作为实盘收益。
- `mock/`：隔离策略进程、合成交易所、账本重放、历史回归入口及全部合成路径证据。
- `evidence/`：冻结版本的校验与本次交接检查。
- `CURRENT_RELEASE.json`、`RELEASE_MANIFEST.json`：版本入口和交付文件哈希。

公开仓库不包含真实账户、真实库存明细、密钥或实盘回执。新增实盘数据经显式授权后另行交付。
