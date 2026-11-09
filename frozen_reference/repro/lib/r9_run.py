"""Round 9: external-audit fixes (claims 1-5). Frozen rule = r7 gate m=2 (funding+basis) + X5 conf_h=48 + derisk pov-respecting, scenario 'cal'.
S from env NFB_S (1e7 / 2e7). Aggregates only; per-coin quantities (token ledger cap) are used internally and never printed."""
import sys, os, json, time; ARGV = list(sys.argv); sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import run_r7 as RR
from run_r7 import *
import pandas as pd
RR.WINS["FY"] = (A0, A1)
OUT9 = os.environ.get("NFB_DAILY", OUT + "r9_daily/").rstrip("/") + "/"; os.makedirs(OUT9, exist_ok=True)
def _qty():
    x = pd.read_csv(os.environ["NFB_CAP_FILE"])   # execution team file: fut_symbol,cap_usdt,cap_qty_token
    q = x.groupby("fut_symbol")["cap_qty_token"].sum().astype(float)
    return np.array([q.get(f, 0.0) for f in D.fut])
QTY = _qty()
CX = dict(f_coin=2.0, fx_fund=2.0, res_mode=2.0, fx_eq=2.0)      # reviewer-requested conservative config: coin+USD cap, full funding charge incl. own impact on e>=0 hours, 3x residual cost, loss-only equity budget
FIX = {
 "baseM": dict(f_mtmderisk=1.0),
 "cons": dict(CX), "consM": dict(CX, f_mtmderisk=1.0),
 "cons_k0": dict(CX, k_imp=0.0), "cons_k2": dict(CX, k_imp=2.0), "consM_k0": dict(CX, f_mtmderisk=1.0, k_imp=0.0), "consM_k2": dict(CX, f_mtmderisk=1.0, k_imp=2.0),
 "allCM": dict(f_coin=2.0, fx_fund=1.0, res_mode=2.0, fx_eq=2.0, f_mtmderisk=1.0),
 "base":  {},
 "f1":    dict(f_coin=1.0),
 "f1m":   dict(f_coin=2.0),
 "f2":    dict(fx_fund=1.0),
 "f2x":   dict(fx_fund=2.0),
 "f3a":   dict(res_mode=1.0),
 "f3b":   dict(res_mode=2.0),
 "f4":    dict(fx_eq=1.0),
 "f4m":   dict(fx_eq=2.0),
 "all":   dict(f_coin=1.0, fx_fund=1.0, res_mode=1.0, fx_eq=1.0),
 "allm":  dict(f_coin=1.0, fx_fund=1.0, res_mode=1.0, fx_eq=2.0),
 "all_3b":dict(f_coin=1.0, fx_fund=1.0, res_mode=2.0, fx_eq=1.0),
 "allx":  dict(f_coin=1.0, fx_fund=2.0, res_mode=2.0, fx_eq=2.0),
 "allC":  dict(f_coin=2.0, fx_fund=1.0, res_mode=2.0, fx_eq=2.0),
 "allCx": dict(f_coin=2.0, fx_fund=2.0, res_mode=2.0, fx_eq=2.0),
 "allC_dlyS": dict(f_coin=2.0, fx_fund=1.0, res_mode=2.0, fx_eq=2.0, leg_dly=1.0),
 "allC_dlyP": dict(f_coin=2.0, fx_fund=1.0, res_mode=2.0, fx_eq=2.0, leg_dly=2.0),
 "dlyS":  dict(leg_dly=1.0),
 "dlyP":  dict(leg_dly=2.0),
 "all_dlyS": dict(f_coin=1.0, fx_fund=1.0, res_mode=1.0, fx_eq=1.0, leg_dly=1.0),
 "all_dlyP": dict(f_coin=1.0, fx_fund=1.0, res_mode=1.0, fx_eq=1.0, leg_dly=2.0),
}
def getfix(name):
    if name in FIX: return dict(FIX[name])
    t = name.split("_"); b = t[0]                                   # cons / consM / consI / consMI [_k<k>] [_r<mult>]
    assert b in ("cons", "consM", "consI", "consMI"), name
    f = dict(CX)
    if "M" in b[4:]: f["f_mtmderisk"] = 1.0
    if "I" in b[4:]: f["imp_size"] = 1.0
    for x in t[1:]:
        if x[0] == "k": f["k_imp"] = float(x[1:])
        elif x[0] == "r": f["res_mult"] = float(x[1:])
        elif x == "dS": f["leg_dly"] = 1.0        # spot leg 1h late
        elif x == "dP": f["leg_dly"] = 2.0        # perp leg 1h late
        else: raise ValueError(name)
    return f
def run9(fix, win):
    f = getfix(fix); ex = dict(conf_h=48.0)
    rk = {}
    if f.get("f_coin", 0) > 0.5: rk["capq"] = QTY
    ex.update(f)
    return RR.run_cfg(("r7", 2.0), ex, "cal", win, True, rkw=rk)
def job9(a):
    fix, win = a; t = time.time(); r = run9(fix, win); o = RR.metrics(r, save=None)
    cum = np.concatenate([[0.0], r["cum"]]); np.save(OUT9 + f"S{int(S/1e6)}M__{fix}__{win}.npy", np.diff(cum))
    dg = r["diag"]; pos = r["pos"]; sc_ = sc[RR.WINS[win][0]:RR.WINS[win][1]]
    with np.errstate(all="ignore"):
        coins = np.where(sc_ > 0, pos / sc_, 0.0); ratio = coins / np.where(QTY > 0, QTY, np.nan)[None, :]
    o.update(fix=fix, win=win, S=S, ann=o["ann10"], resid_n=dg[24], resid_u=dg[8], resid_cost_u=dg[9], unwind_max_h=dg[25], unwind_nobud_n=dg[27],
             peak_mtm_u=dg[13], avg_mtm_u=dg[14] / len(r['cum']), mtm_trim_u=dg[16], unwind_wavg_h=(dg[26] / max(dg[8], 1.0)) if dg[8] > 0 else 0.0, max_coin_ratio=float(np.nanmax(ratio)), share_hours_gt1=float(np.nanmean(ratio[pos > 0] > 1.0001)) if (pos > 0).any() else 0.0,
             notional_share_gt1=float((pos * (ratio > 1.0001)).sum() / max(pos.sum(), 1.0)), sec=time.time() - t)
    return o
if __name__ == "__main__":
    fixes = ARGV[1].split(","); wins = ARGV[2].split(","); nw = int(ARGV[3]); tag = ARGV[4]
    jobs = [(f, w) for f in fixes for w in wins]
    with mp.get_context("fork").Pool(nw) as p: rows = p.map(job9, jobs, chunksize=1)
    df = pd.DataFrame(rows); df.to_csv(OUT + f"r9_{tag}_S{int(S/1e6)}M.csv", index=False)
    print(df[["fix", "win", "ann", "net_u", "avg_cost_used_u", "avg_mtm_used_u", "episodes", "resid_u", "resid_cost_u", "max_coin_ratio", "sec"]].round(2).to_string())
