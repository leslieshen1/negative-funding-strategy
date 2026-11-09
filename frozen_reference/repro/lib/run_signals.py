"""v12 pure engine, signal family E1-E7 x X1-X5 (pre-registered in ../REPORT_signals.md). Aggregates only; nothing per coin is written."""
import sys, os, multiprocessing as mp
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import lib12s as L12
from lib12s import *
import lib12s
OUT = os.environ.get("NFB_OUT", "./out/").rstrip("/") + "/"   # output dir (env NFB_OUT; default = original path)
D = load(); N = D.N; S = float(os.environ.get("NFB_S", 1e7)); T = D.arr[0].shape[0]
ret, oic = spike_arrays(D); ADL_MASK, _ = trigger_mask(ret, oic, 0.10, 0.0, 24)
ev = np.nan_to_num(D.arr[6]); r8 = D.arr[7]; fc = D.arr[4]; sc = D.arr[1]
r8n = np.nan_to_num(r8, nan=0.0)

# ---------- causal entry masks (all use data up to hour t, same timing as r8[t] in the engine) ----------
def shift(a, k, fill=np.nan):
    out = np.full_like(a, fill, dtype=float); out[k:] = a[:-k]; return out
cs = np.cumsum(ev, axis=0)
sum24 = cs - shift(cs, 24, 0.0)                                   # events settled in hours t-23..t
E_all = np.ones((T, N), bool)
sum24n = sum24  # observed settled funding of the last 24h (events in hours t-23..t); causal, same timing as r8[t]
MASK = {"E1": E_all, "E2": E_all, "E3": E_all, "E4": (sum24 < -0.0015) & (r8n < 0)}
# E5: last settled interval shorter than the max interval seen in the prior 30 days
ivh = D.ivh(); ivl = np.zeros((T, N)); cur = np.zeros(N)
for t in range(T):
    m = ivh[t] > 0; cur = np.where(m, ivh[t], cur); ivl[t] = cur
prior_max = np.zeros((T, N))
# explicit rolling max (clear, not clever): prior_max[t] = max(ivl[t-720 .. t-1])
for t in range(T):
    lo = max(0, t - 720); prior_max[t] = ivl[lo:t].max(axis=0) if t > lo else ivl[t]
E5 = (ivl > 0) & (ivl < prior_max)
MASK["E5"] = E5
oi = D.oih; coin_oi = np.where((oi > 0) & (shift(fc, 1) > 0), oi / shift(fc, 1), np.nan)   # oih[k]/close[k-1]: OI snapshot known at start of hour k
cs_ = shift(coin_oi, 1)                                              # use hour t-1's snapshot (same 1h-stale convention as the engine's OI limit)
MASK["E6"] = np.isfinite(cs_) & np.isfinite(shift(cs_, 24)) & (cs_ > shift(cs_, 24))
MASK["E7"] = E_all
ENTRY = {"E1": dict(entry_fav_n=0.0005), "E2": dict(entry_fav_n=0.0010), "E3": dict(entry_fav_n=0.0005, n_streak=2, streak_thr=0.0005, big_thr=0.0),
         "E4": dict(entry_fav_n=1e-9), "E5": dict(entry_fav_n=0.0005), "E6": dict(entry_fav_n=0.0005), "E7": dict(entry_fav_n=0.0005, entry_basis_n=-0.003)}
EXIT = {"X1": dict(xmode=1.0, exit_fav_n=1e-9, wait_max_n=0), "X2": dict(xmode=2.0, exit_fav_n=-1e9, wait_max_n=0), "X3": dict(xmode=3.0, exit_fav_n=-1e9, wait_max_n=0),
        "X4": dict(xmode=4.0, exit_fav_n=-1e9, wait_max_n=0), "X5": dict(xmode=5.0, exit_fav_n=-1e9, wait_max_n=0),
        "X6": dict(xmode=6.0, exit_fav_n=-1e9, wait_max_n=0, x6_thr=0.0001), "X7": dict(xmode=7.0, exit_fav_n=-1e9, wait_max_n=0, x7_h=168.0)}
FIXED = dict(derisk_force=0)

def hrs(date): return int((pd.Timestamp(date, tz="UTC") - pd.Timestamp(C.WINDOWS["1y"][0], tz="UTC")).total_seconds() // 3600)
H_IS = hrs("2026-07-01"); H_ALL = len(range(h0, h1))
def month_edges():
    a = pd.Timestamp(C.WINDOWS["1y"][0], tz="UTC"); b = pd.Timestamp(C.WINDOWS["1y"][1], tz="UTC")
    pts = [a] + list(pd.date_range(a + pd.offsets.MonthBegin(1), b, freq="MS", tz="UTC")) + [b]
    pts = sorted(set(pts)); return [(p, int((p - a).total_seconds() // 3600)) for p in pts]
ME = month_edges()

MODES = {"i": dict(mkm=0), "ii_50": dict(mkm=1, pen=0.002, fshare=0.5), "ii_100": dict(mkm=1, pen=0.002, fshare=1.0),
         "iii_50": dict(mkm=2, pen=0.002, fshare=0.5), "iii_100": dict(mkm=2, pen=0.002, fshare=1.0)}

def spec_run(sp, excl=None):
    ok = ~ADL_MASK
    if excl is not None: ok = ok & ~excl[None, :]
    obs = sp.get("obs", 1); imp = sp.get("imp", 30)
    if sp["e"] == "REF": ex = dict(FIXED)                                  # v11 tuned rule: engine defaults (enter < -0.10%, wait up to 168h / 0.3%)
    else:
        ok = ok & MASK[sp["e"]]
        ex = dict(ENTRY[sp["e"]]); ex.update(EXIT[sp["x"]]); ex.update(FIXED)
        if sp["x"] == "X7": ex["x7_cost"] = 2 * (0.0015 + 2 * imp / 1e4)
    ex["x_obs"] = float(obs)
    if "k" in sp: ex["k_imp"] = float(sp["k"])
    if sp.get("gap_sym"): ex["gap_sym"] = float(sp["gap_sym"])
    ex.update(sp.get("extra", {}))
    md = dict(MODES[sp.get("mm", "i")])
    if "advw" in sp: md["advw"] = sp["advw"]
    return run(D, S, ok_t=ok, extra=ex, imp=imp, dl_stop=False, advmode=1.0, s24=sum24, **md)

def coin_pnl(r):
    ep = r["ep"]
    if not len(ep): return np.zeros(N)
    return np.bincount(ep[:, 0].astype(int), weights=ep[:, 3] - ep[:, 6], minlength=N)

def ep_stats(r):
    ep = r["ep"]; out = {}
    if not len(ep): return out
    o = np.lexsort((ep[:, 1], ep[:, 0])); e = ep[o]; same = np.r_[False, e[1:, 0] == e[:-1, 0]]
    gap = np.where(same, e[:, 1] - np.r_[0, e[:-1, 2]], 1e9)
    out["reentry24_share"] = float((gap <= 24).mean() * 100)
    dur = e[:, 2] - e[:, 1]; net = e[:, 3] - e[:, 6]
    for nm, lo, hi in (("lt24", 0, 24), ("d24_72", 24, 72), ("d72_168", 72, 168), ("gt168", 168, 1e9)):
        m = (dur >= lo) & (dur < hi) if hi < 1e9 else dur >= lo
        out[f"n_{nm}"] = int(m.sum()); out[f"net_{nm}_u"] = float(net[m].sum())
    out["avg_hold_h"] = float(dur.mean())
    out["reentry_lt24_n"] = int((gap < 24).sum()); out["reentry_lt24_share"] = float((gap < 24).mean() * 100)
    for h in (2, 8, 24): out[f"reentry{h}_n"] = int((gap <= h).sum()); out[f"reentry{h}_share"] = float((gap <= h).mean() * 100)
    return out

def metrics_row(r):
    cum = np.concatenate([[0.0], r["cum"]]); H = len(cum) - 1; y = H / 8760.0
    row = dict(net_u=cum[-1], ann=cum[-1] / S / y * 100, episodes=r["episodes"])
    isn = cum[H_IS]; ho = cum[-1] - cum[H_IS]
    row.update(is_net_u=isn, is_ann=isn / S / (H_IS / 8760.0) * 100, ho_net_u=ho, ho_ann=ho / S / ((H - H_IS) / 8760.0) * 100)
    q = R.quarterly(r)
    for i, (_, g) in enumerate(q.iterrows()): row[f"Q{i+1}_ann"] = g.ann_on_S_pct; row[f"Q{i+1}_net_u"] = g.net_wan * 1e4
    s = summ(r, S); row.update(funding_u=s["funding_wan"] * 1e4, impact_u=s["cost_wan"] * 1e4, fees_u=s["fees_wan"] * 1e4)
    row["fees_impact_u"] = -(row["fees_u"] + row["impact_u"]); row["price_u"] = row["net_u"] - row["funding_u"] - row["fees_u"] - row["impact_u"]
    row["avg_used_u"] = float(r["used"].mean()); row["residual_end_u"] = r["diag"][8]
    x = r["xd"]; row.update(sig_exits=int(x[46]), sig_exits_negfund=int(x[47]), gap_cost_u=x[35], adverse_u=x[36], hedge_extreme_u=x[37], maker_pair_u=x[32])
    row["exit_negfund_share"] = x[47] / x[46] * 100 if x[46] > 0 else np.nan
    row.update(ep_stats(r)); row["other_liq_u"] = x[48] + x[49]
    row["basis_u"] = row["price_u"] - row["other_liq_u"]      # mid-to-mid price PnL (price_u + cost) excluding delisting/end-of-window liquidation
    dg = r["diag"]; Hh = len(r["used"]); row["avg_cost_used_u"] = dg[15] / Hh; row["peak_cost_used_u"] = dg[12]; row["peak_mtm_used_u"] = dg[13]
    row["hrs_mtm_gt_S_pct"] = float((r["used"] > S * 1.0001).mean() * 100)
    u = r["used"]; row["avg_used_u"] = float(u.mean()); row["peak_used_u"] = float(u.max())
    row["avg_used_is_u"] = float(u[:H_IS].mean()); row["avg_used_ho_u"] = float(u[H_IS:].mean())
    row["ann_on_avg_used"] = row["net_u"] / max(row["avg_cost_used_u"], 1.0) * 100
    for i in range(len(ME) - 1): row[f"M{ME[i][0].strftime('%Y-%m')}_u"] = cum[ME[i+1][1]] - cum[ME[i][1]]
    return row

def task(t):
    name, sp = t
    r = spec_run(sp); row = metrics_row(r); row.update(name=name, e=sp["e"], x=sp["x"], imp=sp.get("imp", 30), obs=sp.get("obs", 1), k=sp.get("k", 4), mm=sp.get("mm", "i"), gap_sym=int(bool(sp.get("gap_sym"))))
    pnl = coin_pnl(r); srt = np.sort(pnl)[::-1]; row["top5_share"] = srt[:5].sum() / pnl.sum() * 100 if pnl.sum() > 0 else np.nan
    row["coins_traded"] = int((np.bincount(r["ep"][:, 0].astype(int), minlength=N) > 0).sum()) if len(r["ep"]) else 0
    if sp.get("excl"):
        order = np.argsort(-pnl); ex = np.zeros(N, bool); ex[order[:5]] = True
        m2 = metrics_row(spec_run(sp, excl=ex))
        for k in ("ann", "net_u", "is_ann", "ho_ann", "is_net_u", "ho_net_u", "episodes", "Q1_ann", "Q2_ann", "Q3_ann", "Q4_ann"): row[f"ex5_{k}"] = m2[k]
    return row

def pool(specs):
    with mp.get_context("fork").Pool(8) as p: return pd.DataFrame(p.map(task, specs, chunksize=1))

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "check":                                     # new engine, obs=0, must reproduce the first-round numbers
        for e, x in (("E1", "X1"), ("E7", "X3")):
            r1 = metrics_row(spec_run(dict(e=e, x=x, imp=30, obs=0))); print(e, x, round(r1["ann"], 3), r1["episodes"], round(r1["exit_negfund_share"], 1))
    if mode == "main":                                      # A (obs=0, X1-X5) and B (obs=1, X1-X7) full grids, 3 impact levels, + tuned reference
        specs = []
        for obs, xs in ((0, ["X1", "X2", "X3", "X4", "X5"]), (1, list(EXIT))):
            for e in ENTRY:
                for x in xs:
                    for imp in (20, 30, 50): specs.append((f"{e}|{x}|{imp}|obs{obs}", dict(e=e, x=x, imp=imp, obs=obs)))
            for imp in (20, 30, 50): specs.append((f"REF|REF|{imp}|obs{obs}", dict(e="REF", x="REF", imp=imp, obs=obs)))
        pool(specs).to_csv(OUT + "grid2.csv", index=False)
    g = pd.read_csv(OUT + "grid2.csv") if mode != "check" else None
    if mode in ("ex", "k", "maker"):
        m = g[(g.imp == 30) & (g.obs == 1) & (g.e != "REF")].sort_values("is_ann", ascending=False)
        top = [(r.e, r.x) for r in m.head(5).itertuples()]
    if mode == "ex":
        specs = [(f"{e}|{x}|30|ex", dict(e=e, x=x, imp=30, obs=1, excl=True)) for e, x in top] + [("REF|REF|30|ex", dict(e="REF", x="REF", imp=30, obs=1, excl=True)),
                 ("REF|REF|30|ex_obs0", dict(e="REF", x="REF", imp=30, obs=0, excl=True))]
        specs += [(f"{e}|{x}|30|ex_obs0", dict(e=e, x=x, imp=30, obs=0, excl=True)) for e, x in
                  [(r.e, r.x) for r in g[(g.imp == 30) & (g.obs == 0) & (g.e != "REF")].sort_values("is_ann", ascending=False).head(5).itertuples()]]
        pool(specs).to_csv(OUT + "ex5_2.csv", index=False)
    if mode == "k":
        specs = []
        for k in (0, 2, 4, 8):
            for e, x in top + [("REF", "REF")]:
                for obs in (1, 0): specs.append((f"{e}|{x}|30|k{k}|obs{obs}", dict(e=e, x=x, imp=30, obs=obs, k=k)))
        pool(specs).to_csv(OUT + "ksens.csv", index=False)
    if mode == "maker":
        specs = []
        for e, x in top[:1] + [("REF", "REF")]:
            for mm in ("i", "ii_50", "ii_100", "iii_50", "iii_100"):
                for gs in (0, 1):
                    if mm == "i" and gs: continue
                    specs.append((f"{e}|{x}|30|{mm}|gs{gs}", dict(e=e, x=x, imp=30, obs=1, mm=mm, gap_sym=gs)))
        pool(specs).to_csv(OUT + "maker2.csv", index=False)
