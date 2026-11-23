# 复现

只需 Python 3.9+ 标准库。命令在仓库根目录执行。示例是合成状态，不是账户、库存或收益证据。本包不联网，也不连接交易接口。

```sh
python3 -B -m unittest discover -s fusion_a/tests -v
python3 -B -m fusion_a.cli configure --config fusion_a/examples/synthetic/config.json
python3 -B -m fusion_a.cli cycle --config fusion_a/examples/synthetic/config.json --snapshot fusion_a/examples/synthetic/snapshot.json
```

`configure` 只解结构身份。示例是含借币总 AUM 的 50,000,000 参数、USDT 权重 0.55，所以现金预算是 27,500,000，借币市值是 22,500,000。这不是已到账余额。

`cycle` 对合成快照给出绝对 target。示例里的费用、冲击和完成率是写在输入里的合成标定，其中费率显式为 0，只为让状态机可运行；它们不是实测成本。在 20% 虚拟安全留存、10% 单币集中度和 1 倍现货加保证金约束下，目标现货数量为 `50000`、永续数量为 `-50000`，计划新占现金 `10000000`。输出同时满足：

- `submission_allowed` 为 false
- `execution_actions` 为空数组
- `targets_are_not_fills` 为 true
- `pnl_is_not_evidence` 为 true
- `return_evidence` 为 false
- 基线 commit 为 `6b98320bfb707c903212e26888cef79a1c12342f`

部分成交示例回执：

```sh
python3 -B -m fusion_a.cli cycle --config fusion_a/examples/synthetic/config.json --snapshot fusion_a/examples/synthetic/snapshot.json > /tmp/fusion-a-cycle.json
python3 - << 'PY'
import json
doc = json.load(open("/tmp/fusion-a-cycle.json"))
json.dump(doc["ledger"], open("/tmp/fusion-a-ledger.json", "w"))
PY
python3 -B -m fusion_a.cli apply --config fusion_a/examples/synthetic/config.json --ledger /tmp/fusion-a-ledger.json --receipt fusion_a/examples/synthetic/partial_fill.receipt.json
```

`apply` 只更新被成交的那一条腿。另一条腿保持原数量。

已覆盖的合成不变量：25/50/100M 与 USDT 50/55/60% 的两种资本身份；申赎不进盈亏；重试耗尽后停止新增；过期快照不新增；挂单在撤单确认前继续占用；期末负债仍在且账面残差为 0、年化为 null；后续快照不改变已经发出的前缀 target。缺费用时不出现默认 bps。这些结果只证明状态守恒。

回归冻结基线：

```sh
python3 -B scripts/verify_release.py
python3 -B -m unittest discover -s runtime/tests -v
python3 -B -m unittest discover -s support/tests -v
```

## 还不能当作上线证据

实盘方仍需提供：部署 commit、实际运行的配置哈希、脱敏的 position/universe 快照、target 回执、成交与费用、借还币、理财申赎、保证金和 MMR。在这些材料按同一状态机重放之前，不能把任何输出称为已验证融合收益或生产许可。
