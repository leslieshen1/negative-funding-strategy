# RC2 策略侧参考模块

策略 `nfb.strategy.20261108.rc2`；协议2.1.0。执行侧仍收发JSON。本目录提供可离线运行的r7收入／成本过滤器、受约束决策函数、冻结模型快照、Schema和校验器。

运行仅依赖Python3.9及以上标准库。执行侧也可按公式、快照及样例在现有语言中实现；不要求执行引擎调用交易所之外的网络服务。`jsonschema==4.25.1`只用于可选的独立合同测试。

```sh
python3 -S verify_package.py
python3 -S decision.py --input runtime_examples/01_historical_market_enter.input.json --replay
```

这是一条历史行情、合成账户的计算命令，不发送订单。默认不使用历史时钟；真实输入调用时去掉 `--replay`，程序按实际接收时钟拒绝过期请求。`--replay`不能与live模式混用。所有输出仍为 `submission_allowed=false`。

| 文件 | 用途 |
|---|---|
| `decision.py` | 单次输入状态→JSON决策，可通过`--output`写文件 |
| `online_scorer.py` | r7联合／边际收入估计、成本、波动率和ADL代理 |
| `models/r7-model-bundle.json` | 14个更新节点、资产先验、4613条公开成交校准摘要 |
| `MODEL_PROFILE.json` | 风控参数、策略标识和固定模型快照哈希 |
| `schemas/` | 输入、输出、真实执行回执Schema |
| `validate.py` | 报文结构、时点、数量与风险约束校验 |
| `runtime_examples/` | 9组由实际决策函数生成的固定输入／预期输出 |
| `examples/` | 保留的38个合成合同消息，仅测试协议，不是实际模型的计算输出 |
| `联调验收.md` | E01—E14持久化、并发、乱序与故障场景 |

输入每币可附 `model_history`：26条连续小时记录，包含bar_end_ms、received_at_ms、spot_close_usdt_per_token、perp_close_usdt_per_token、oi_usdt及availability_source。缺少时不新增仓位，已有退出锁及风险减仓仍继续计算。原币库存、真实账户和挂单预留仍按Schema提供，不能用借币申请或预计到账量代替。

模型快照有实际发布时间和有效月份。普通计算不得在发布时间之前使用该快照；历史重放明确标记proxy来源。新月份没有模型时停止新增，不自动沿用旧月份。11月快照已补齐，当前有效期到2026年12月1日。

成本校准最新样本为2026年9月30日，价格与冲击仍有模型假设。输出带`calibration_age_hours`和旧月份提醒；该模块没有把这些假设变成真实成交成本。

输出的目标、数量和资金意图不是实际成交。重复请求、退出锁持久化、并发额度预留、订单生命周期和实际回执仍须既有系统实现与验收。本模块不写生产账本、不下单、不借币、不划转。

文件完整性按`SHA256SUMS.txt`的原始文件字节检查。内嵌配置／模型哈希另用文件声明的Python规范JSON编码；其他语言接入不应随意重序列化数值后生成一个不同哈希。
