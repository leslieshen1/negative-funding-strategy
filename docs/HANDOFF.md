# 执行方交接：输入 → 决策 → 真实回执

接口链路：行情及实际账户状态 → 策略/风控计算 → JSON 决策 → 执行方处理 → 真实回执 → 更新账本与复盘。

## 唯一字段定义

| 消息 | 权威 Schema | 固定样例 |
|---|---|---|
| 策略输入 | [strategy-input.schema.json](../runtime/schemas/strategy-input.schema.json) | [历史行情+合成账户输入](../runtime/runtime_examples/01_historical_market_enter.input.json) |
| 决策输出 | [strategy-output.schema.json](../runtime/schemas/strategy-output.schema.json) | [对应预期输出](../runtime/runtime_examples/01_historical_market_enter.output.json) |
| 执行回执 | [execution-report.schema.json](../runtime/schemas/execution-report.schema.json) | [合同样例目录](../runtime/examples/) |
| 资金补充输入/输出 | [补充 Schema](../support/schemas/) | [资金风险样例](../support/examples/) |

## 输入方需要提供什么

顶层包括 `protocol_version`、`message_type`、`request_id`、`as_of_ms`、`ledger_version`、`mode`、`model`、`portfolio`、`instruments`。逐币提供真实合约映射、实际双腿持仓、已结算费率、行情、库存、挂单/资金预留、风控状态和26条连续小时 `model_history`。

时间戳为 UTC 毫秒；金额与数量为十进制字符串。现货原币数量与永续交易所数量通过 `tokens_per_perp_quantity` 对齐；1000倍合约不得直接比较两腿交易所数量。实际到账库存确定可用量，申请或预计到账不能当库存。

## 输出方表达什么

`input_request_id` 和 `ledger_version` 关联输入；`valid_until_ms` 控制有效期。每个 `targets` 项表达 ENTER / ADD / REDUCE / EXIT / HOLD / RECONCILE：

- `desired_pair_qty_token`：最终目标。
- `target_pair_qty_token`：本轮受约束后的目标。
- `delta_pair_qty_token`：本轮差量。
- `target_perp_qty_exchange`、`tokens_per_perp_quantity`：合约单位换算。
- `legs`、`max_slice_notional_usdt`：允许的两腿切片与上限。
- `exit_locked`、`risk_state`、`reason_codes`：持久化退出和风险动作。
- `transfer_requests`：资金意图，实际到账前不增加接收账户可用余额。

## 执行方返回什么

回执包括 `report_id`、`decision_id`、`ledger_version_before/after`、实际 `fills`、`funding_receipts`、`transfer_receipts`、`coin_return_receipts` 及 `positions_after`。部分成交、拒单、费用与延迟必须保留。买回原币不自动等于提供方确认归还；内部划转不计利润。

执行方负责订单生命周期、幂等、并发预留、撤单确认、补腿和实际资金流转。我方根据回执做持仓、现金、费用、资金费和债务对账。详见 [E01—E14](../runtime/联调验收.md) 与 [执行方验收模板](../support/EXECUTOR_ACCEPTANCE_TEMPLATE.json)。

## 验收顺序

先核对版本与哈希，再复跑固定输入和输出，再完成 E01—E14 的持久化、并发、乱序和故障用例，最后由执行方回填真实日志。当前本地测试通过不能替代执行方验收；本包输出持续为 `submission_allowed=false`。
