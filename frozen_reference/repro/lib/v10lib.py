"""v10 harness: one causal 1y run, then slice cumulative PnL (incl. mark-to-market) at the tune/test split. Nothing is re-fitted after the split."""
import os, sys, time
os.environ.setdefault("NFB_DATA_DIR", os.environ["NFB_DATA"])
import numpy as np, pandas as pd
import config as C
import run_backtest as R

SPLIT = "2026-07-01"        # tune: 1y window start -> 2026-06-30 ; test: 2026-07-01 -> data end 2026-10-27 (window end 2026-10-28 excl.)
CASES = {"optimistic": dict(lag=0, imp=20, k=0.0), "realistic": dict(lag=1, imp=30, k=4.0)}
SGRID = [1e6, 2e6, 3e6, 5e6, 7.5e6, 1e7, 1.5e7, 2e7, 3e7, 4e7, 5e7, 7e7]


def slice_metrics(r, S):
    """r = result of a 1y run. returns dict with tune / test / 1y net U and annualised % on S (cum includes MTM)"""
    a, b = C.WINDOWS["1y"]; h0 = R.hidx(a); hs = R.hidx(SPLIT) - h0
    cum = r["cum"]; H = len(cum)
    yt = hs / 24 / 365.0; ys = (H - hs) / 24 / 365.0; y1 = H / 24 / 365.0
    c0 = cum[hs - 1]; c1 = cum[-1]
    used = r["used"]
    out = {}
    for nm, pnl, yy, uu in (("tune", c0, yt, used[:hs]), ("test", c1 - c0, ys, used[hs:]), ("1y", c1, y1, used)):
        out[f"{nm}_net_wan"] = pnl / 1e4; out[f"{nm}_ann_pct"] = pnl / S / yy * 100; out[f"{nm}_avg_used_wan"] = uu.mean() / 1e4
    return out


def run_cfg(D, S, ok, cap, params, lag, ok_m=None, cap_m=None, windows=("1y", "6m")):
    res = {}
    for w in windows:
        r = D.run(w, S, ok, cap, params, oi_extra_lag_h=lag, ok_m=ok_m, cap_m=cap_m)
        m = R.metrics(r)
        row = dict(window=w, net_wan=m["net_wan"], ann_pct=m["ann_on_S_pct"], avg_used_mtm_wan=m["avg_used_mtm_wan"], peak_used_mtm_wan=m["peak_used_mtm_wan"],
                   peak_pct_of_S=m["peak_used_mtm_pct_of_S"], episodes=m["episodes"], funding_wan=m["funding_wan"], basis_wan=m["basis_gross_wan"],
                   cost_wan=m["spread_impact_wan"], fees_wan=m["fees_wan"])
        if w == "1y": row.update(slice_metrics(r, S))
        res[w] = row
    return res


def fix_oi_zero(D, maxrun=3):
    """Data-quality fix (v10 r2): an hourly OI of exactly 0 that lasts <= maxrun hours and is preceded by a positive value is a feed glitch (not 'unknown'): forward-fill it
    with the last positive value. Longer zero runs, leading zeros and NaN are left untouched (still 'unknown -> cannot hold'). Returns number of coin-hours filled."""
    oi = D.oih; T, N = oi.shape; n_fill = 0
    for j in range(N):
        col = oi[:, j]; z = np.where(col == 0)[0]
        if not len(z): continue
        runs = np.split(z, np.where(np.diff(z) > 1)[0] + 1)
        for rn in runs:
            a, b = rn[0], rn[-1]
            if len(rn) <= maxrun and a > 0 and np.isfinite(col[a - 1]) and col[a - 1] > 0:
                col[a:b + 1] = col[a - 1]; n_fill += len(rn)
    return n_fill
