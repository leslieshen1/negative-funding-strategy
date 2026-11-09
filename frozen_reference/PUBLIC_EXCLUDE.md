# 公开仓库排除清单(不得进入任何公开仓库)

- team_report/evstat/events.pkl —— 含逐币币名与币池标记
- team_report/evstat/sett.pkl —— 含逐币结算明细
- team_report/evstat/ev2.py —— 含私有路径
- team_report/evstat/mkt/fund.pkl 及 mkt/ 下其他原始数据
- 任何持仓/库存数量、Borrowable、单币上限文件

公开仓只放汇总数字(stats.json 汇总字段、图表、md/pdf)。

## v13(待 Leslie 决定前一律不公开)
- `v13_next/out/a/taus.pkl`:按币序的每币首次触发时刻。
- `v13_next/out/b/B_monthly.csv`:含 `med_capsum_x`(上限之和÷本金),配合 1000 万可反推上限合计规模。

- v15 private: `PROFIT_internal.md`（含币名）、`*.bak31`、`*.bak_iv`、`v15_profit/out/`（含 tables.pkl、随机编号种子 .id_seed 与编号映射 id_map_PRIVATE.csv）
- *.bak* （所有备份文件，不得推送）

- v14 private: `v14_types/out/`（events_typed_nocoin.pkl 已删 ci/strat/stratH 列，但 features.pkl 含事件索引 i，可与 events.pkl 联表）、`*.bak_iv14` 备份

- `**/v15_profit/out/`
- `.id_seed`
- `id_map_*`
- v14_types/out/events_typed_nocoin.pkl 和 features.pkl：永不公开。虽然文件名带 nocoin、已删掉币名列，但开始时间、持续时长、最深费率、结算间隔、成交额、OI、收入以及 lvol/loi 等字段可对照公开资金费历史反推出币种。

## 内部完整表（禁止公开）
- `v15_profit/PROFIT_full_internal.xlsx`、`PROFIT_full_internal.pdf`、`PROFIT_full_internal_top20.png`（真实币名、逐笔时间）
- `v15_profit/code/p_full_internal.py`、`p_pdf_internal.py`、`p_pdf_build.py`、`v15_profit/pdf/`（内部图与中间文件）
