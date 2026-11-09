# 策略支持补充包 v1

本包用于内网JSON联调、策略侧离线风控和回执分析。正式策略仍为 `nfb.strategy.20261108.rc2`、协议 `2.1.0`。资金补充协议另用 `nfb.treasury-input.v1` / `nfb.treasury-output.v1`，不把它们混入旧Schema。

执行方继续使用已有下单、拆单、撤单、补腿和资金划转系统。本包Python是参考计算/校验工具，执行引擎也可在现有语言实现相同JSON合同。代码没有交易所账户、下单、借币、资金划转或发消息的网络接口。

## 文件

| 文件 | 作用 |
|---|---|
| `schemas/treasury-input.schema.json` | 生息资产、账户可用资金、原币义务和权限输入 |
| `schemas/treasury-output.schema.json` | 收紧新增额度、退出锁、核对、赎回/划转待到账意图 |
| `schemas/execution-report.schema.json` | 原RC2回执Schema副本 |
| `examples/*.input.json`、`*.expected.json` | 7组固定风控输入与预期输出；全部为合成数据 |
| `examples/opening.synthetic.json` | 合成期初账本与1000倍合约映射 |
| `examples/roundtrip.synthetic.execution-report.json` | 开仓、资金费、划转、平仓、现货买回、确认归还的合成回合 |
| `treasury_risk.py` | 单次资金风控决策，只能收紧RC2许可 |
| `receipt_ledger.py` | SQLite持久化参考账本、请求/消费/事件去重和原子预留 |
| `reconcile_cli.py` | 接收给定回执，在指定本地数据库计算账本并与独立状态比较 |
| `execution_analytics.py` | 将给定成交与下单前报价匹配，分腿分析成交偏差和费用 |
| `forward_log.py` | 手动前瞻留痕、拒绝回填、固定窗口检查；不创建自动任务 |
| `EXECUTOR_ACCEPTANCE_TEMPLATE.json` | E01—E14执行方回填模板；本地通过与执行方通过分开 |
| `frozen_identity.json` / `v3_binding.json` | RC2身份与v3模型/源码绑定，不触发模型训练 |
| `tests/` | 离线行为检查，不连接执行系统 |

## 离线运行

除前瞻分数重算要使用已有v3研究环境外，其余参考工具只需Python标准库。

```sh
python3 -B verify_support.py
python3 -B -m unittest discover -s tests -v
python3 -B treasury_risk.py --input examples/01_eligible.input.json --replay
python3 -B reconcile_cli.py --database paper.sqlite --opening examples/opening.synthetic.json --reports examples/roundtrip.synthetic.execution-report.json --output paper-result.json
```

`--replay`只供历史/合成样例计算；正常输入调用去掉该参数，以实际接收时钟检查过期。所有示例保持 `submission_allowed=false`。对账命令仅写显式指定的本地数据库、原始回执证据与结果文件。

## 资金补充输入与输出

金额和数量均为十进制字符串，不使用科学记数法。USDT现金、USDC、生息资产单位与原币单位必须区分。LDUSDT数量须乘当时NAV后估值，不能固定当成等量USDT。合约数量须按原始乘数折回原币。

输入的核心字段：

| 字段 | 含义 |
|---|---|
| `as_of_ms` / `snapshot_received_at_ms` | 决策时点与实际收到完整账户快照时点 |
| `ledger_version` | 与对应策略请求一致的账本版本；参考账本版本不能直接冒充生产版本 |
| `account_mode` / `account_kind` | 普通单币、联合保证金、PM或PM Pro；独立主账户或普通子账户 |
| `subaccount_conversion_route_verified` | 普通子账户的主账户转换/赎回路径是否已核对 |
| `account_a` | 已到账USDT和已有预留 |
| `account_b` | 已到账USDT、预留、浮盈亏、其他负债、当前名义仓位、交易所调整后权益、真实可划转及新订单保证金额度 |
| `yield_assets` | 账户归属、真实数量、NAV、抵押折扣/分层、是否已到账、权限、观察时间与有效期 |
| `pending_redemptions` / `pending_transfers` | 赎回与划转在途；不计可花现金或B的可用抵押品 |
| `instruments` | 实际卖出现货量、永续原币量、借入/已确认归还量、回购报价、退出锁、风险事件和再利用权限 |
| `rc2_new_exposure_budget_usdt` | RC2已经准许的新增上限；资金补充层不得扩大它 |

输出只有决策意图，不是成交结果：

| 输出 | 接入要求 |
|---|---|
| `allow_new_exposure=false` / `max_new_exposure_usdt=0` | 暂停开仓和加仓；已有风险退出继续处理 |
| `LOCK_EXIT` | 保留最终退出目标，执行方按实际容量/价格/余额拆单；缺报价不制造成交 |
| `RECONCILE_HEDGE` | 对照两腿实际数量处理敞口，不能补记成完整对冲 |
| `REQUEST_TRANSFER_WAIT_FOR_SETTLEMENT` | 必须等真实到账；请求本身不增加余额 |
| `REDEEM_TO_CASH_WAIT_QUOTE_AND_SETTLEMENT` | 先确认实际费用、额度和转换路径，再等真实USDT到账 |
| `KEEP_IDLE_COINS_UNENCUMBERED` | 未确认再出借/质押权限时保留原币，不产生额外负担 |

执行时同时满足RC2和资金层的限制，额度取更小值，有效期取更早值；核对同一账本版本、最新报价和账户快照。A的抵押品不直接记入B，正浮盈不直接当可划转USDT；PM Pro缺少分层信息时停止新增。

## 回执、对账与并发

真实回执是唯一成交事实。`positions_after`等外部快照需与事件账本独立对照；本地计算通过不等于生产账本已验收。使用 `--expected-state` 可比对独立提供的现金、原币、债务和敞口状态。

`receipt_ledger.py`保留收到的事件、冲突/缺前因隔离记录和重复投递信息。晚到成交仍入账；缺少较早开仓回执时先隔离，后续可恢复。内部划转不计利润。`RESTORED_IN_A`不等于`RETURNED_TO_PROVIDER`，买回也不自动减少提供方债务。

原RC2的PENDING划转不足以证明源账户已经扣款。若需精确表达已扣未到，请提供补充 `nfb.ledger-event.v1` 的DEBITED、SETTLED、FAILED、REFUNDED阶段；失败不自动等于退款到账。补充事件需与真实账户/资金回执一致。

参考预留使用SQLite原子事务。每个消费切片必须有唯一 `consumption_id`；重放不重复消费。撤单未确认保留预留，撤单确认仍保留已消费风险，直到执行侧实际仓位预算接管。现有引擎必须独立证明自身持久化、并发、订单和预算更新，不应把这段参考代码的PASS当成其验收。

## 成交成本分析输入

```sh
python3 -B execution_analytics.py --report supplied.execution-report.json --benchmarks preorder-quotes.json --output cost-analysis.json
```

`preorder-quotes.json`为 `{"source_mode":"observed","rows":[...]}`。每条按account、market、symbol、trade_id匹配，包含quote_event_time_ms、quote_received_at_ms、order_submit_time_ms、bid_usdt_per_exchange_quantity、ask_usdt_per_exchange_quantity、fee_asset_usdt_price。

必须满足报价实际收到时刻≤下单≤成交。缺报价、旧报价、合约单位错误和无法换算的手续费分别保留，不默认为0。成交相对下单前中价的偏差包含市场移动和逆向选择，不足以因果识别自身价格或资金费影响k。

## 手动v3前瞻

新台账不读取或覆盖旧的预测台账。真实记录需在固定00UTC日网格后5分钟内取得可追溯快照；这是防止回填的记录时限，不是重训或新增交易门槛。已经发生在实际发出预测前的目标事件单独处理，不能算提前命中。

记录真实预测需提供原始request、原v3计算输出、原始源文件SHA和绑定模型SHA，并指定已有v3候选目录和可运行该候选的Python环境。CLI会独立重算冻结分数后核对；没有当前125特征快照时不生成预测。

```sh
python3 -B forward_log.py --database new-v3.sqlite --request observed.request.json --prediction v3.output.json --evidence source-evidence.json --candidate-directory /path/to/frozen-v3 --scorer-python /path/to/research-python
```

固定标签：新转负72小时、持续性168小时、借币72小时后机会至240小时、经济参考至1008小时。缺失、迟到和删失不作为负样本；未成熟拒绝提前写结果。接收的外部标签仍标为待独立重算，工具不会自动把它们变成已验证命中率。本包不自动抓取、不定时评分、不变更阈值。

## 仍需外部验收

实际产品报价/额度/权限、执行方内网E01—E14、真实成交与归还、实际补腿/清算/ADL，以及新v3前瞻效果，均未由本包验收。执行方按既有合同与本补充JSON联调后返回日志即可，联调不要求真实下单。
