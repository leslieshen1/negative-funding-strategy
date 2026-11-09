"""Causal watchlist rules. pool[h,n] uses ONLY data up to hour h-1 (r8, closes, OI, volume of hours <= h-1).
 c = instantaneous condition; pool = c true at least once in the trailing L hours (the coin stays borrowed / on the list L hours after the last hit; it is removed afterwards).
Thresholds are the funding levels 0 / -2bp / -5bp / -10bp per 8h (-10bp = the engine's entry-grade threshold) and windows 7d / 30d; basis -50/-100bp; OI +50% in 7d; volume 3x 7d median."""
import numpy as np, pandas as pd
D_ = 24
def lag1(x, fill=0.0):
    o = np.vstack([np.full((1, x.shape[1]), fill), x[:-1]]); return o
def trail_any(c, L):
    p = np.vstack([np.zeros((1, c.shape[1]), np.int32), np.cumsum(c.astype(np.int32), axis=0)]); T = c.shape[0]
    idx = np.arange(T); lo = np.maximum(idx - L + 1, 0)
    return (p[idx + 1] - p[lo]) > 0
def trail_mean(x, L):
    p = np.vstack([np.zeros((1, x.shape[1])), np.cumsum(x, axis=0)]); T = x.shape[0]
    idx = np.arange(T); lo = np.maximum(idx - L + 1, 0)
    return (p[idx + 1] - p[lo]) / (idx + 1 - lo)[:, None]

def build_rules(D):
    r8 = np.nan_to_num(D.arr[7], nan=0.0); r8l = lag1(r8)                       # latest funding known at the start of hour h-1 -> usable at h
    sc, fc = D.arr[1], D.arr[4]
    with np.errstate(all="ignore"):
        basis = np.where((sc > 0) & (fc > 0), (fc - sc) / sc, 0.0)
    basl = lag1(basis)
    oi = np.nan_to_num(D.oih, nan=0.0)                                           # OI of the previous completed hour (already causal)
    oi7 = np.vstack([np.zeros((168, oi.shape[1])), oi[:-168]])
    with np.errstate(all="ignore"):
        oig = np.where((oi7 > 0) & (oi > 0), oi / oi7, 0.0)
    dv = D.dv.astype(float)                                                     # trailing 24h volume (t-24..t-1)
    med7 = pd.DataFrame(dv).rolling(168, min_periods=96).median().shift(24).to_numpy()   # median of the 7d before the last day (causal)
    with np.errstate(all="ignore"):
        vs = np.where(med7 > 0, dv / med7, 0.0)
    R = {}
    for tag, x in (("neg0", 0.0), ("low2", 0.0002), ("low5", 0.0005), ("low10", 0.001)):
        for Ld, nm in ((168, "7d"), (720, "30d")):
            R[f"{tag}_{nm}"] = trail_any(r8l < -x if x > 0 else r8l < 0, Ld)
    R["mean3d_lt2_7d"] = trail_any(trail_mean(r8l, 72) < -0.0002, 168)
    R["mean7d_lt2_7d"] = trail_any(trail_mean(r8l, 168) < -0.0002, 168)
    R["basis50_7d"] = trail_any(basl < -0.0050, 168)
    R["basis100_7d"] = trail_any(basl < -0.0100, 168)
    R["oigrow50_7d"] = trail_any(oig > 1.5, 168)
    R["volspike3x_7d"] = trail_any(vs > 3.0, 168)
    R["U1_low5_30d+basis100"] = R["low5_30d"] | R["basis100_7d"]
    R["U2_low2_30d+basis50+oi+vol"] = R["low2_30d"] | R["basis50_7d"] | R["oigrow50_7d"] | R["volspike3x_7d"]
    R["U3_low10_30d+basis100"] = R["low10_30d"] | R["basis100_7d"]
    R["ALL(no_lag_ref)"] = np.ones_like(R["neg0_7d"])
    return R
