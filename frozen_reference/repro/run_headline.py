"""唯一入口：头条配置（10M 本金，cons 保守成本，k_imp=4，入场门槛 m=2，0h 出场 = v1 主线规则）的 IS / HO / FY 年化。
环境变量：NFB_DATA（含 ds/ 与 calib/）、NFB_CAP_FILE（执行团队提供的逐币上限 CSV）、NFB_ROOT（输出根目录，默认当前目录）。
不打印任何逐币信息；分档统计只按"入场那一小时已知的 8h 等效预期资金费率 r8"汇总。

参数（全部预先登记，看结果前定好）：
  --fee    vip3（默认）| vip0（仅审计复现 51.07%）；币安官方费率表 2026-11-04 查询，不含 BNB 抵扣和任何促销
           vip3: 现货 0.040%/0.060%，U 本位永续 0.0120%/0.0320%   (maker/taker)
  --exec   taker（默认，两腿都吃单）| perp_maker（永续挂单+现货吃单对冲）| spot_maker（现货挂单+永续吃单对冲）
           | tiered_perp / tiered_spot：入场时 r8<=-0.5%（T3/T4）用 taker，其余用对应 maker（按笔拼接）
  --fill   maker 可成交量里实际成交的比例（排队折扣），默认 0.5；网格 0.25/0.5/0.75/1
  --adv    逆向选择：每笔 maker 成交多付 adv x (小时最高-最低)/2，默认 0.5；网格 0/0.5/1
  --adv-mode range（默认，advmode=3：adv x 小时半振幅）| nextclose（advmode=0：maker 腿限价到下一小时收盘的不利变动 x adv）| basis（advmode=2：基差变化，仅双腿都挂单时计，单腿挂单下为 0，仅作对照）
  --hedge  taker（默认：maker 腿成交后，另一腿同一小时按吃单模型价=小时 VWAP+校准价差/冲击）| worst（压力测试：按本小时最差价）
  --gap    off（默认：未成交部分下一小时两腿一起吃单，价格变动对冲后不计额外成本）| on（旧口径：单边计入价格不利变动，仅压力测试）
  --entry-tiers  只允许在这些档位入场（如 T2,T3,T4），用于"分档信号"对比，默认全部
  --json   把结果（含分档统计）写到这个文件
分档（入场时 r8，8h 等效）：T1 > -0.1%；T2 -0.1%~-0.5%；T3 -0.5%~-1%；T4 <= -1%。
复现：--fee vip0 --exec taker 必须得到 FY 51.07%（旧头条）。"""
import os, sys, json, argparse
ap = argparse.ArgumentParser()
ap.add_argument("--fee", default="vip3", choices=["vip0", "vip3"])
ap.add_argument("--exec", default="taker", choices=["taker", "perp_maker", "spot_maker", "tiered_perp", "tiered_spot"])
ap.add_argument("--fill", type=float, default=0.5); ap.add_argument("--adv", type=float, default=0.5)
ap.add_argument("--entry-tiers", default="T1,T2,T3,T4"); ap.add_argument("--json", default=None)
ap.add_argument("--windows", default="IS,HO,FY")
ap.add_argument("--adv-mode", default="range", choices=["range", "nextclose", "basis"]); ap.add_argument("--hedge", default="taker", choices=["taker", "worst"]); ap.add_argument("--gap", default="off", choices=["off", "on"])
A = ap.parse_args()
for k in ("NFB_DATA", "NFB_CAP_FILE"):
    if not os.environ.get(k): sys.exit(f"请先设置环境变量 {k}（见 README）")
ROOT = os.environ.get("NFB_ROOT", ".").rstrip("/")
os.environ["NFB_OUT"] = ROOT + "/out/"; os.environ["NFB_DAILY"] = ROOT + "/out/daily/"
os.makedirs(os.environ["NFB_DAILY"], exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import engine_v12s, lib12s           # 带逐笔记账字段的引擎副本，必须先于其它模块导入
sys.argv = sys.argv[:1]
import numpy as np, v13lib
from v13lib import run_fix, ann_of
from run_signals import r8n

FEES = {"vip0": dict(fee_s=0.001, fee_s_mk=0.001, fee_f=0.0005, fee_f_mk=0.0002),
        "vip3": dict(fee_s=0.0006, fee_s_mk=0.0004, fee_f=0.00032, fee_f_mk=0.00012)}
TIERS = [("T1", -0.001, 9.0), ("T2", -0.005, -0.001), ("T3", -0.01, -0.005), ("T4", -9.0, -0.01)]   # (lo, hi]
def tier_of(x):
    out = np.empty(x.shape, object)
    for nm, lo, hi in TIERS: out[(x > lo) & (x <= hi)] = nm
    return out
MKM = {"perp_maker": 1.0, "spot_maker": 3.0}
S = 1e7

def run(mode, win, mask):
    mko = dict(FEES[A.fee])
    if mode != "taker": mko.update(mkm=MKM[mode], pen=0.002, fshare=A.fill, advw=A.adv, advmode={"range": 3.0, "nextclose": 0.0, "basis": 2.0}[A.adv_mode], hx_off=(1.0 if A.hedge == "taker" else 0.0), gap_sym=(2.0 if A.gap == "off" else 0.0))
    v13lib._ns["MKO"].clear(); v13lib._ns["MKO"].update(mko)
    r = run_fix("cons_c0", win, mask=mask); ep = r["ep"]
    net = ep[:, 3] - ep[:, 6]; tr = tier_of(r8n[ep[:, 1].astype(int), ep[:, 0].astype(int)]); hold = ep[:, 2] - ep[:, 1]
    tiers = {nm: dict(n=int((tr == nm).sum()), net_wan=float(net[tr == nm].sum() / 1e4), hold_h=float(hold[tr == nm].mean()) if (tr == nm).any() else 0.0) for nm, _, _ in TIERS}
    H = len(r["cum"]); xd = r["xd"]
    return dict(ann=ann_of(r), n=len(ep), H=H, net_wan=r["cum"][-1] / 1e4, fund_wan=r["funding"] / 1e4, fees_wan=r["fees"] / 1e4, price_wan=r["price"] / 1e4,
                cost_wan=r["cost"] / 1e4, traded_wan=r["traded"] / 1e4, gap_wan=xd[35] / 1e4, tiers=tiers)

allow = set(A.entry_tiers.split(","))
mask = None
if allow != {"T1", "T2", "T3", "T4"}:
    tt = tier_of(r8n); mask = np.isin(tt.astype(str), list(allow))     # 只用当小时已知的 r8 判断，可否入场
out = dict(args=vars(A))
for w in A.windows.split(","):
    if A.exec.startswith("tiered"):
        a = run("taker", w, mask); m = run(A.exec.replace("tiered", "").strip("_") + "_maker", w, mask)
        net = sum((a if k in ("T3", "T4") else m)["tiers"][k]["net_wan"] for k in ("T1", "T2", "T3", "T4"))
        o = dict(ann=net * 1e4 / S / (a["H"] / 8760) * 100, net_wan=net, n=a["n"], H=a["H"],
                 tiers={k: (a if k in ("T3", "T4") else m)["tiers"][k] for k in ("T1", "T2", "T3", "T4")}, note="按笔拼接：T3/T4 取 taker 运行，T1/T2 取 maker 运行")
    else:
        o = run(A.exec, w, mask)
    out[w] = o
    print(f"{w}: 年化 {o['ann']:.2f}%  笔数 {o['n']}  净利 {o['net_wan']:.1f} 万U  | " + "  ".join(f"{k} n={v['n']} {v['net_wan']:.1f}万" for k, v in o["tiers"].items()), flush=True)
if A.json:
    out["engine_params"] = {k: (float(v) if isinstance(v, (int, float, np.floating)) else str(v)) for k, v in v13lib._ns["LASTP"].items() if not hasattr(v, "shape")}
    json.dump(out, open(A.json, "w"), indent=1, default=float)
