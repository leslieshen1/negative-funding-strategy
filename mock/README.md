# RC2闭环mock与已知历史回归

`run_mock.py`把未来路径只留在合成交易所中；策略独立JSONL子进程只接收当前/过去的请求和模拟时钟。它是代码级输入隔离，不是操作系统安全沙箱，也不是研究者未见历史数据的独立实盘盲测。无网络、真实订单和真实预测账本写入。模型、阈值不变。

```sh
python3 -B mock/run_mock.py --output /tmp/nfb-mock-new
python3 -B mock/reconcile_receipts.py --mock-root /tmp/nfb-mock-new --output /tmp/nfb-ledger-new
```

输出目录必须是新的。首轮结束才公开seed；复现已公开路径时，从`evidence/strategy-replay/REVEALED_SEED.json`读取seed，加`--seed VALUE`。复现不再是未知随机路径。`first-sealed-run/`保留首轮哈希承诺；扩展断言后只重放相同seed，不挑选新情景。

合成两币使用冻结映射（含1000倍合约），每路径144h。八类固定情景：正常、部分成交、断腿、旧报价、退出锁后费率再负、提前还币、流动性骤降、划转延迟。执行方补腿只用mock替身，不代表策略已实现补腿或执行方验收。

`reconcile_receipts.py`调用真实`support/receipt_ledger.py`，重放合成成交、资金费、实际到账划转及归还。初次normalizer在“仅现货成交、没有永续成交”的路径停止；失败证据留在本机，新版不补造缺失成交。SQLite仅写调用者指定目录，不进Git。

完整历史回归有外部资产依赖，不下载、不填造、不训练：

```sh
python3 -B mock/run_historical_regression.py --data /path/to/authorized/market-root --inventory /path/to/private/inventory_caps.csv --output /tmp/nfb-historical-new
```

需安装原研究依赖（见信号库锁定requirements），市场文件和库存哈希必须匹配报告；文件缺失停止。公开仓库不包含私有库存，无法只靠克隆获得相同私有场景的完整回测。

13.19%与5.86%的控制回放见[专项报告](../docs/RETURN_RECONCILIATION_20261110.md)，入口`reconcile_month_returns.py`。`reference/month_replay_original.py`是按字节保留的原研究来源快照，仅由包装器在新目录内运行，勿直接当作当前数据采集器或生产入口。
