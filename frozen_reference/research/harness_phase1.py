"""v11 harness (basis A only). Loads the merged dataset (267 pool + 74 extension), runs engine_v11 with taker / maker modes, borrow variants, ADL filter, counters."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NFB_DATA_DIR", os.environ["NFB_DATA"])
from pathlib import Path
import numpy as np, pandas as pd
import config as C, run_backtest as R, v10lib as V
mkp = None
import engine_v12s as E
mkp = E.mkp
from rules import build_rules, trail_any
from adl_signals import spike_arrays, trigger_mask

DS = Path(os.environ["NFB_DATA"]) / "ds"
HC = os.environ["NFB_CAP_FILE"]   # execution team file: fut_symbol,cap_usdt,cap_qty_token
CASE = dict(lag=1, imp=30, k=4.0)
CFG_V10 = dict(pov=0.03, derisk_force=1)
LAG_H = 72
h0, h1 = R.hidx(C.WINDOWS["1y"][0]), R.hidx(C.WINDOWS["1y"][1]); HS = R.hidx(V.SPLIT)
XDN = {}   # filled by names below
ENTRY_POLICY = None  # headline sets this before loading the research pipeline

class DD(R.Data):
    pass

def load():
    D = R.Data(DS)
    z = np.load(DS / "dataset.npz")
    D.ohlc = {k: z[k].astype(np.float64) for k in ("so", "sh", "sl", "fo", "fh", "fl")}
    m = D.meta
    D.tk_s = m.tick_s.to_numpy(float) if "tick_s" in m else np.zeros(D.N); D.tk_f = m.tick_f.to_numpy(float)
    D.tk_s = np.nan_to_num(D.tk_s); D.tk_f = np.nan_to_num(D.tk_f)
    D.dl_exit = m.dl_exit.to_numpy(float); D.dl_fin = m.dl_fin.to_numpy(float); D.delisted = m.delisted.to_numpy(bool)
    D.grp = m.grp.to_numpy()
    if ENTRY_POLICY is not None:
        D.entry_r8 = ENTRY_POLICY.r8
        D.entry_threshold = -ENTRY_POLICY.metadata['threshold_r8']
        D.entry_recheck_mask = ENTRY_POLICY.mask
        D.extra_adl_filter = ENTRY_POLICY.metadata.get('extra_adl_filter', True)
        D.expected_exit_confirmation_hours = ENTRY_POLICY.metadata.get('expected_exit_confirmation_hours', 24)
    return D

def cap_vec(D, S, universe="all"):
    from inventory import load_caps
    h = load_caps(HC); hm = dict(zip(h.fut_symbol, h.cap_usdt))
    u = np.array([min(hm.get(f, 0.0), S) for f in D.fut])
    if universe == "pool": u = np.where(D.grp == "pool", u, 0.0)
    return u

def avail_keep(pool, lag=LAG_H):
    """variant (a) 'borrow and keep': once in the pool the borrow is never returned; usable `lag` hours after the first membership hour."""
    T, N = pool.shape; first = np.where(pool.any(0), pool.argmax(0), 10**9)
    return np.arange(T)[:, None] >= (first + lag)[None, :]
def avail_return(pool, lag=LAG_H):
    """variant (b) previous rule: coin leaves the pool 30d after the last hit, the borrow is returned, clock restarts."""
    T, N = pool.shape; p = pool.astype(np.int32)
    c = np.vstack([np.zeros((1, N), np.int32), np.cumsum(p, axis=0)]); out = np.zeros((T, N), bool)
    for t in range(lag, T): out[t] = (c[t + 1] - c[t - lag]) == lag + 1
    return out

def profile(D, days=14):
    """VWAP weights: past-only same-hour-of-day volume profile, prof[t] = mean over d=1..days of min(spot,perp volume)[t-24d]  (the current hour is never used)"""
    if getattr(D, "_prof", None) is None:
        v = np.minimum(np.nan_to_num(D.arr[2]), np.nan_to_num(D.arr[5])); T, N = v.shape; acc = np.zeros((T, N)); cnt = np.zeros((T, 1))
        for d in range(1, days + 1):
            if 24 * d < T: acc[24 * d:] += v[:T - 24 * d]; cnt[24 * d:] += 1
        D._prof = acc / np.maximum(cnt, 1)
    return D._prof

MKO = {}
LASTP = {}
def run(D, S, mkm=0, pen=0.002, pen_k=0.0, fshare=1.0, advw=1.0, ok_t=None, oilim_mask=None, universe="all", extra=None, window="1y", imp=None, dl_stop=False, advmode=0.0, s24=None, hw=None, same_hour=False, xa=None, capq=None, S_eff=None):
    kw = dict(CFG_V10); kw.update(extra or {}); kw.update(mkm=float(mkm), pen=pen, pen_k=pen_k, fshare=fshare, advw=advw, advmode=advmode); kw.update(MKO)
    hh0, hh1 = (h0, h1) if hw is None else hw
    signal_end = hh1
    tail_hours = int(kw.pop("tail_hours", 0))
    if tail_hours < 0: raise ValueError("tail_hours must be nonnegative")
    if not (0 <= hh0 < hh1 <= len(D.arr[0])): raise ValueError("dataset does not cover the requested window")
    if tail_hours:
        hh1 = min(hh1 + tail_hours, len(D.arr[0]))
        kw["trade_end"] = float(signal_end)
    if same_hour: kw['f_prior'] = 0
    kimp = kw.pop("k_imp", CASE["k"])
    params = R.base_params(CASE["imp"] if imp is None else imp, k_imp=kimp, **kw)
    LASTP.clear(); LASTP.update(params)
    arr = D.causal_min_arrays() if same_hour else D.arr; stk = D.streak_arr(params.pop("streak_thr", 0.001)); P = mkp(capital=S, **params)
    T = arr[0].shape[0]; N = D.N
    oi = D.oih; oi = np.vstack([np.full((1, N), np.nan), oi[:-1]])
    if same_hour:                                    # OI cap on the same-hour actual OI: min(prior-hour snapshot, start-of-hour, end-of-hour)
        oc = D.oih; on = np.vstack([oc[1:], np.full((1, N), np.nan)])
        oi = np.fmin(np.fmin(oi, oc), on)
    oilim = np.where(np.isfinite(oi), getattr(D, 'oi_cap_fraction', 0.10) * oi, 0.0)
    dvv = D.dv
    if same_hour:                                    # 10% trailing-24h volume cap including the current hour: min(t-24..t-1, t-23..t)
        x = np.minimum(np.nan_to_num(D.arr[2]), np.nan_to_num(D.arr[5])); c = np.vstack([np.zeros((1, N)), np.cumsum(np.nan_to_num(D.arr[2]), axis=0)]); c2 = np.vstack([np.zeros((1, N)), np.cumsum(np.nan_to_num(D.arr[5]), axis=0)])
        inc = np.zeros((T, N)); inc[23:] = np.minimum(c[24:T + 1] - c[:T - 23], c2[24:T + 1] - c2[:T - 23]); dvv = np.where(inc > 0, np.minimum(D.dv, inc), D.dv)
    if oilim_mask is not None: oilim = np.where(oilim_mask, 0.0, oilim)
    cap = cap_vec(D, S, universe)
    okn = np.ones((T, N), np.uint8) if ok_t is None else ok_t.astype(np.uint8)
    okn = okn * (cap > 0).astype(np.uint8)[None, :]
    H = hh1 - hh0; used = np.zeros(H); cum = np.zeros(H); comp = np.zeros((H, 6)); epb = np.zeros((200000, 11)); diag = np.zeros(32); pos_rec = np.zeros((H, N)); xd = np.zeros(64)
    accounts = np.zeros((H, 9)) if params.get("res_mode", 0) > 2.5 and params.get("leg_dly", 0) == 0 and params.get("mirror_on", 0) == 0 else None
    o = D.ohlc
    res, blocked, lost_f = E.sim(hh0, hh1, *arr, dvv, oilim, D.hs_s, D.hs_f, D.r2, D.r5, okn, np.zeros((T, N), np.uint8), cap, np.zeros(N), D.mk, (np.zeros(N) if capq is None else np.asarray(capq, float)),
                                 D.oiv_for("hour", CASE["lag"]), D.ivh(), D.sigma(), stk, P, used, cum, epb, comp, diag, pos_rec,
                                 o["so"], o["sh"], o["sl"], o["fo"], o["fh"], o["fl"], D.tk_s, D.tk_f, (D.dl_exit if dl_stop else np.full(D.N, 1e9)), D.dl_fin, xd, np.zeros((T, N)) if s24 is None else s24, profile(D), np.ones((3, T, N)) if xa is None else xa, getattr(D, "fsm", np.ones(D.N)), getattr(D, "entry_r8", None), accounts, True, getattr(D, "entry_recheck_mask", None), getattr(D, 'entry_score', None))
    price, fund, fees, cost, traded, nf, ne, ar = res.sum(axis=0)
    return dict(S=S, price=price, funding=fund, fees=fees, cost=cost, traded=traded, impact=2.0 * params.get("impact", 0.0) * traded, episodes=int(ne), fills=int(nf), lost_f=lost_f,
                diag=diag, used=used, cum=cum, comp=comp, ep=epb[:int(min(ne, len(epb)))], signal_hours=signal_end-hh0, tail_hours=hh1-signal_end, years=R.years_of(window), pos=pos_rec, xd=xd, cap=cap, window=window, accounts=accounts,
                resolved_engine_parameters={name:float(P[i]) for i,name in enumerate(E.PN)})

def summ(r, S):
    m = R.metrics(r); sl = V.slice_metrics(r, S)
    return dict(ann=m["ann_on_S_pct"], tune_ann=sl["tune_ann_pct"], test_ann=sl["test_ann_pct"], net_wan=m["net_wan"], tune_net_wan=sl["tune_net_wan"], test_net_wan=sl["test_net_wan"],
                avg_used_wan=sl["1y_avg_used_wan"], episodes=r["episodes"], funding_wan=m["funding_wan"], cost_wan=m["spread_impact_wan"], fees_wan=m["fees_wan"])

def delisted_split(D, r):
    ep = r["ep"]
    if not len(ep): return dict(n_ep=0, net_wan=0.0, coins=0)
    cn = ep[:, 0].astype(int); m = D.delisted[cn]; net = ep[:, 3] - ep[:, 6]
    return dict(n_ep=int(m.sum()), net_wan=float(net[m].sum() / 1e4), coins=int(len(np.unique(cn[m]))), n_ep_all=len(ep), net_all_wan=float(net.sum() / 1e4))
