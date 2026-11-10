# 向执行方申请的A/B账户资料 · 2026-11-10

请先提供A/B同一时点的脱敏完整快照，以及一组完整事件样本；后续提供快照＋增量事件。我们要管理的是现金、库存、两腿、挂单预留和还币负债，账户总资产数字不能代替这些状态。

真实样本通过双方私有通道提供，不放GitHub或群里的公开文件。由执行方维护交易所凭证并发送数据即可，不要求把交易/提现密钥交给我方。脱敏account_id须稳定，仍能区分A/B和主/子账户关系。

## 一次性基础资料

| 信息 | 具体内容 | 为什么需要 |
|---|---|---|
| 账户关系 | A/B实际用途、独立主账户或同主账户普通子账户、服务地区/交易所实体 | 决定资金和生息路径；不同账户权益不能自动互抵 |
| 账户模式 | 单币/联合保证金/PM/PM Pro、全仓/逐仓、单向/双向、实际杠杆 | 1x研究配置不证明账户真实设置 |
| 权限及通道 | 谁能交易、划转、申赎；A→B/B→A通道、限额、费用、到账状态定义 | 独立主账户不等于同主账户子账户内部划转 |
| 费率及规格 | A/B逐市场VIP/返佣/BNB折扣，合约乘数、步长、最低量/金额、风险档位 | 数量、费用和可下单金额要逐币核对 |
| 提供方口径 | 原币归还还是现金结算、借币费/起息、到期、召回通知、用途、收益归属/亏损承担 | 决定负债、可生息期限及净利润；“币到账”不代表拥有该币 |
| 初始账本 | 各币本金/债务、策略与其他业务的仓位/现金归属、起始ledger_version | 不能把借入/划转记作收益，也不能接管别的业务头寸 |

## A账户：原币、现货和回购资金

| 申请内容 | 必须分开的字段 | 现有合同映射 |
|---|---|---|
| 现金 | USDT/USDC等各资产free、locked、reserved；已到账与在途 | `portfolio.account_a_free_usdt`；资金补充`account_a.settled_usdt/reserved_usdt` |
| 实际币余额 | token、total/free/locked、托管/理财/质押数量、估值时点 | 可动用量汇入`limits.inventory_available_qty_token`，不能把total全算可卖 |
| 借币及还币 | provider/loan/lot标识、借入量、当前债务本金及利息、已卖、未卖、回购到A、已请求归还、提供方已确认归还 | `episode_coin_return_due_token`；补充`borrowed_qty_token/provider_confirmed_return_qty_token`；完整债务明细在适配层保留 |
| 订单与预留 | client_order_id、交易所order_id、方向、总量/成交/剩余、状态、冻结资产、归属策略/episode | `reserved_entry_cost_usdt`、`reserved_add_qty_token`等由实际订单映射 |
| 币/U理财 | product_id、资产、数量、借入/自有来源、净APR/分层、质押占用、赎回额度/费用、申请与到账时间 | `yield_assets`、`pending_redemptions`；原币回收能力另保留明细 |
| 召回与限制 | 召回量/通知时间/截止时间、禁止交易/提现/赎回状态 | `risk.force_exit/pause_new`、notice_received_at、补充risk_events |

A的原币数量核对：借入本金−提供方确认归还=未偿还本金；交易、内部转账、买回、赎回都不能单独减少该负债。利息按合同另记应付，不和本金混淆。多笔借入按lot分配，每笔归还只能核销一次。

## B账户：永续、保证金和可划转资金

| 申请内容 | 必须分开的字段 | 现有合同映射 |
|---|---|---|
| 资产和负债 | 各币钱包/现金、权益、未实现盈亏、已实现盈亏、借款和应付费用 | `account_b_wallet_usdt/equity_usdt`；补充`unrealized_pnl_usdt/other_liabilities_usdt` |
| 真实可用额度 | 可用于新订单保证金、可划转/可提现、已预留/冻结、跨业务占用 | `account_b_free_usdt/transferable_usdt`；补充`exchange_available_for_new_initial_margin_usdt/exchange_max_withdraw_usdt` |
| 保证金 | 交易所当前初始/维持保证金、保证金率、风险档位、抵押资产及折扣/分层、清算/ADL状态 | 补充`exchange_adjusted_equity_usdt/yield_assets`；维持保证金/ADL原始字段由适配器保留 |
| 实际逐合约持仓 | symbol、positionSide、方向、有符号交易所量、合约乘数、原币量、entry/mark/liquidation price、notional、leverage、marginType | 当前RC2只接收所管组合的long量；原始有符号多空头寸必须完整保留，不能隐藏其他策略空头 |
| 挂单及未结束请求 | 订单/撤单/部分成交、剩余量、reduceOnly、占用、client_order_id和决策关联 | 共享预留及切片预算；撤单请求不是撤单成功 |
| 资金费及现金事件 | event_id、结算时点、当时持仓/mark、实际收付、费用币种、收到时点 | `funding_receipts`、fills和资金账本；与预测费率分开 |

永续交易所数量乘`tokens_per_perp_quantity`才是原币量。1合约单位对应1000原币的市场，不能直接拿两个交易所quantity比较。若以后加入正向策略，B会出现short：现RC2 long-only字段不能直接塞负数；须单独策略版本/适配层和子账本，原始账户风控仍合并计算。

## 两账户共同的持续事件

| 类型 | 最低内容 | 特别要求 |
|---|---|---|
| 成交 | trade_id、order/client_order_id、account、market/symbol、side、交易所量/原币量/乘数、成交价、实际费额/币种、event/received时间 | 所有部分成交、撤单后晚到成交、拒单都保留；同trade_id不能重复记账 |
| 成交成本证据 | 下单前两腿bid/ask和可见档位、报价event/received时点、submit/ack/fill/cancel时点 | 报价先被收到，再下单；最好覆盖订单金额及若干固定深度，不用成交后盘口冒充下单前盘口 |
| 划转 | transfer_id、来源/去向、资产、请求/源扣款/目标到账/失败/退回时间、费用 | DEBITED不等于SETTLED；失败不等于已退款；内部划转不计利润 |
| 借还币 | loan/lot、token、原币数量、借入/到账、回购、归还申请/提供方确认、利息和费用 | `RESTORED_IN_A`和`RETURNED_TO_PROVIDER`分开 |
| 申赎及奖励 | 产品、持仓单位、NAV、费用、收益资产、请求/实际到账、抵押占用 | NAV单位不得按1:1写死；理财在途不当现金 |
| 同步及故障 | source、snapshot_id、sequence、event_time、received_at、ledger_version、完整性、缺口、断线/重连/对账结果 | 全量快照与事件流校验；未知/缺失不填0 |

时间使用UTC毫秒；金额和数量用十进制字符串，保留原始资产及单位。报价、账户、成交和状态时间分别保留。字段缺失不自动推断“无负债/零持仓”。

传输频率草案：成交/订单/资金/风险变化即时发送，完整快照建议1秒或账户变化即发，另做定期全量校验；频率待执行系统能力确认。无论推送频率如何，现RC2的完整输入最大年龄10秒、决策TTL60秒不变。断线/缺序/过期暂停新增暴露，退出和应急通过双方已有风险路径处理并留痕。

## 请先发的一组样本

1. A/B同一时点完整脱敏快照，含当前模式和资产/负债。
2. 一币的借入到账→现货卖出/永续买入→实际资金费→平仓/回购→提供方确认还币完整链条。尚无实盘时可用明确标为synthetic的样本做字段联调，不能当实盘验收。
3. 一笔A→B和一笔B→A资金请求的全生命周期样本；没有真实通道则先说明。
4. 一组部分成交、撤单、晚到成交和重复回执，以及断线后的恢复快照。
5. 每币提供方本金、费用、期限和召回口径，以及候选理财产品只读报价。余额为0也要由完整快照明确给出。

[字段申请CSV](../handoff/account-data-request.template.csv)可逐项回填，真实值/日志另走私有渠道。现有权威合同：[RC2输入](../runtime/schemas/strategy-input.schema.json)、[回执](../runtime/schemas/execution-report.schema.json)、[资金补充输入](../support/schemas/treasury-input.schema.json)、[资金与账本说明](../support/README.md)。原始源字段不适合直接加入冻结Schema时，在独立适配层保留，版本转换后再传RC2。
