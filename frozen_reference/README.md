# 负资金费率策略：第一部分研究

本分支检验冻结的 v1 规则，补充历史数据、独立账本和风险限制。研究日期 2026-11-07；资本和收益分母均为 10,000,000 USDT，借币利息为零。执行引擎由外部团队负责，本项目不下单或转账。

优先阅读 [策略规则](docs/STRATEGY_RULES.md) 和 [模型边界](docs/MODEL_LIMITS.md)。原仓库说明保留在 [V1_ORIGINAL_README.md](docs/V1_ORIGINAL_README.md)，其中历史收益不是本分支的复现结果。

## 冻结版本

- 原始代码：`6cd6c23d6af5e0b92a3840fe44e09655d8adaed0`。
- 同一旧输入上，原代码仅把入场门槛改为严格低于 −0.1%/8h，独立复跑为 75.16414173387383%；包含估算尾仓，不能当作全部兑现收益。原说明的 52.76% 未取得匹配原始输入，尚未复现。
- 新输入：2025-07-01 至 2026-11-06 UTC，不含末时点；339 个档案配对品种，336 个在研究年度内有配对行情。主交易窗口为 2025-10-28 至 2026-10-28，之后只退出、不新增，观察尾段 216 小时。
- 优先研究候选：[candidate_diversified.json](profiles/candidate_diversified.json)。这是策略研究参数，不是生产执行协议；`research_only=true`、`submission_allowed=false`。

## 本地复跑

已验证 CPython 3.12.13。依赖版本见 [requirements.txt](research/requirements.txt)。下面的路径由研究者设置，库存文件必须保留在受控目录中：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r research/requirements.txt
export NFB_PHASE1_ROOT=/absolute/path/to/phase1-data-and-results
export NFB_PRIOR_ROOT=/absolute/path/to/neg-funding-v1-independent-replay-20261106
export NFB_CAP_FILE=/absolute/private/path/inventory_caps.csv
export NFB_INVENTORY_TSV=/absolute/private/path/provided-inventory.tsv

.venv/bin/python research/run_study.py --data "$NFB_PHASE1_ROOT/data" --suite concentration --profile candidate_diversified --output "$NFB_PHASE1_ROOT/concentration"
.venv/bin/python -m unittest discover -s research -p test_phase1.py -v
```

旧基线的数值等价桥接使用 `--data "$NFB_PRIOR_ROOT/data" --suite bridge`。它会断言 75.16414173387383%，并核对扩展引擎的净损益、资金费、手续费及价差损益。

完整数据补充流程：

```bash
.venv/bin/python research/acquire_phase1.py
.venv/bin/python research/complete_acquisition.py
.venv/bin/python research/build_phase1.py
.venv/bin/python research/audit_inputs.py
```

`NFB_PRIOR_ROOT` 的旧 315 币行情、ag.csv、bd.csv 是明确依赖。采集流程在其基础上补充数据，不是从空目录重建旧研究包。旧数据包、私有库存、382MB 数据矩阵和全部原始档案不放入源代码压缩包；输入 SHA256 和源 URL 在证据中保留。重新下载可能因交易所修订而产生新哈希，需要作为新输入版本运行。

研究套件：`lifecycle` 为统一公告处理后的单项对照；`followup` 为流动性组合、压力和首笔 T−1h 对照；`concentration` 为单币限制及压力对照。后续套件是看过诊断结果后的探索性研究，没有新的未观察样本外期。

报告、验收清单、逐案例 results.json、逐月损益、分钟抽查及证据包位于 `NFB_PHASE1_ROOT`。全部数值都是小时级模拟；盘口、异步成交、历史保证金档位及原币身份等边界见模型说明。
