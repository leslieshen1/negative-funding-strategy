"""Round 7: calibrated per-coin per-hour cost + expected-income (funding + basis convergence) gate + exit research. 10M, inventory A, hard caps (derisk_force=1), taker. Aggregates only."""
import sys; sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from r7_lib import *
import json, os
DF = float(os.environ.get("NFB_DERISK", "2"))   # 2 = forced de-risk obeys pov over hours (fixed engine); 1 = legacy same-hour cut-back outside the budget
OUT7 = OUT + "r7_daily/"; os.makedirs(OUT7, exist_ok=True) if os.environ.get("NFB_OUT") else None
YK, HM, CINFO, CRAW = build_cal("kyle"); YB, _, _, _ = build_cal("book")
XEV_CACHE = {}
def xa_for(scen):
    if scen == "cal": Y, H_ = YK, HM
    elif scen == "book": Y, H_ = YB, HM
    elif scen == "sqrt1": Y, H_ = np.ones((T, N)), np.ones((T, N))
    else: return None, None, None
    x = np.ones((3, T, N)); x[0] = Y; x[1] = H_
    return x, Y, H_
XA = {s: xa_for(s) for s in ("cal", "book", "sqrt1")}
for s in ("cal", "book", "sqrt1"): XA[s][0][2] = ev_ratio(XA[s][1], XA[s][2], True)       # EV ratio (funding + basis) for the scenario's own cost
XA_NB = {s: ev_ratio(XA[s][1], XA[s][2], False) for s in ("cal", "book", "sqrt1")}
WINS = {"IS": (A0, A_IS), "HO": (A_IS, A1)}
BASEX = dict(ENTRY["E7"]); BASEX.update(EXIT["X5"]); BASEX.update(FIXED); BASEX["x_obs"] = 1.0; BASEX["conf_h"] = 24.0
def make_ok(gate_spec, scen):
    if gate_spec is None or gate_spec == "none": return ~ADL_MASK
    if gate_spec == "old": return ~ADL_MASK & gate(3, 24)
    kind, m = gate_spec                                   # ("r7", m) funding+basis ; ("r7f", m) funding only, calibrated cost
    xa, Y, H_ = XA["cal"]                                  # the frozen rule always gates with the primary (calibrated) cost; the scenario only changes the REALISED execution cost
    return ~ADL_MASK & gate_r7(m, Y, H_, basis=(kind == "r7"))
def run_cfg(gate_spec, exit_extra, scen, win, ev_basis=True, rkw=None):
    ok = make_ok(gate_spec, scen); ex = dict(BASEX); ex.update(exit_extra or {})
    if scen in ("bp30", "bp30c"):
        ex["derisk_force"] = DF; xa = None; imp = 30
        if scen == "bp30c": ex["cutb_sqrt"] = 1.0                 # legacy cut-backs: excess over pov charged extrapolated sqrt (Y=1, no calibration)
    else:
        ex.update(derisk_force=DF, sqrt_y=1.0, cutb_sqrt=1.0, use_xa=1.0); xa = XA[scen][0]
        if not ev_basis and ex.get("x_ev_k", 0) > 0:
            xa = xa.copy(); xa[2] = XA_NB[scen]
        imp = 30
    return run(D, S, ok_t=ok, extra=ex, imp=imp, dl_stop=False, advmode=1.0, s24=sum24, hw=WINS[win], same_hour=True, xa=xa, **MODES["i"], **(rkw or {}))
def metrics(r, save=None):
    cum = np.concatenate([[0.0], r["cum"]]); H = len(cum) - 1; y = H / 8760.0; ep = r["ep"]; net = cum[-1]; dg = r["diag"]
    o = dict(days=H / 24, net_u=net, episodes=int(r["episodes"]), funding_u=r["funding"], fees_u=-r["fees"], slip_u=-r["cost"], traded_u=r["traded"],
             peak_cost_used_u=dg[12], peak_mtm_used_u=dg[13], avg_cost_used_u=dg[15] / H, avg_mtm_used_u=float(r["used"].mean()),
             volcap_hours=dg[0], held_hours=dg[11], oicap_hours=dg[1], cutback_notional_u=r["xd"][43] if False else 0.0, legacy_cut_u=r["xd"][44], legacy_cut_excess_u=r["xd"][45], trim_hours=r["xd"][52], resid_over_cap_u_hours=r["xd"][53], max_over_cap_ratio=r["xd"][54], resid_hours=r["xd"][55], cut_target_u=r["xd"][56], zero_cap_hours=r["xd"][57], resid_h_lt125=r["xd"][58], resid_h_125_2=r["xd"][59], resid_h_2_5=r["xd"][60], resid_h_gt5=r["xd"][61])
    o["ann10"] = net / S / y * 100
    o["ann_pkc"] = net / o["peak_cost_used_u"] / y * 100 if o["peak_cost_used_u"] > 0 else np.nan
    o["ann_pkm"] = net / o["peak_mtm_used_u"] / y * 100 if o["peak_mtm_used_u"] > 0 else np.nan
    o["basis_u"] = net - o["funding_u"] - o["fees_u"] - o["slip_u"]
    o["volcap_pct"] = 100 * dg[0] / dg[11] if dg[11] > 0 else np.nan
    if len(ep):
        dur = ep[:, 2] - ep[:, 1]; en = ep[:, 3] - ep[:, 6]
        for nm, lo, hi in (("lt24", 0, 24), ("d24_72", 24, 72), ("d72_168", 72, 168), ("gt168", 168, 1e18)):
            mm = (dur >= lo) & (dur < hi); o[f"n_{nm}"] = int(mm.sum()); o[f"net_{nm}_u"] = float(en[mm].sum())
        cp = np.bincount(ep[:, 0].astype(int), weights=en, minlength=N); top = np.sort(cp)[::-1][:5]
        o["ex5_ann"] = float(cp.sum() - top.sum()) / S / y * 100; o["top5_share"] = float(top.sum() / cp.sum() * 100) if cp.sum() != 0 else np.nan; o["avg_hold_h"] = float(dur.mean())
    if save is not None: np.save(OUT7 + save + ".npy", np.diff(cum))
    return o
def job(a):
    name, gate_spec, exit_extra, scen, win = a[:5]; evb = a[5] if len(a) > 5 else True
    r = run_cfg(gate_spec, exit_extra, scen, win, evb)
    o = metrics(r, save=f"{name}__{scen}__{win}"); o.update(cfg=name, scen=scen, win=win); return o
def pool_run(jobs, n=8):
    with mp.get_context("fork").Pool(n) as p: return p.map(job, jobs, chunksize=1)
