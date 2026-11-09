"""ADL-risk proxies (public data only; no liquidation / ADL feed is available).
 spike hour e (perp): hourly perp close-to-close return ret[e] > X  AND  OI measured in COINS (OI_usdt / perp close) fell by > Y during hour e (a drop of open contracts while price jumps ~ shorts being force-closed).
 OI(e) = hourly snapshot at e:55 (oih[e+1]); the series used by the engine is 1h staler in the realistic case -> signal usable from hour e+2 (OI lag) ."""
import numpy as np
def spike_arrays(D):
    fc = D.arr[4]; T, N = fc.shape
    prev = np.vstack([fc[:1], fc[:-1]])
    with np.errstate(all="ignore"):
        ret = np.where((fc > 0) & (prev > 0), fc / prev - 1, 0.0)
        oi = np.nan_to_num(D.oih, nan=0.0)
        oi_end = np.vstack([oi[1:], oi[-1:]])                    # OI snapshot at the end of hour e
        oi_beg = oi                                              # snapshot at the end of hour e-1
        c_end = np.where((oi_end > 0) & (fc > 0), oi_end / fc, np.nan)
        c_beg = np.where((oi_beg > 0) & (prev > 0), oi_beg / prev, np.nan)
        oichg = c_end / c_beg - 1                                # change in open contracts (coins) during hour e
    return ret, oichg
def trigger_mask(ret, oichg, X, Y, K, delay=2):
    """returns (T,N) bool: hour t is 'ADL-risk' if a spike hour e (ret>X and OI-coin change < -Y; Y=None -> price only) occurred with e+delay <= t < e+delay+K."""
    sp = ret > X
    if Y is not None: sp = sp & np.isfinite(oichg) & (oichg < -Y)
    T, N = sp.shape; p = np.vstack([np.zeros((1, N), np.int32), np.cumsum(sp.astype(np.int32), axis=0)])
    t = np.arange(T); hi = t - delay; lo = np.maximum(t - delay - K + 1, 0)
    out = np.zeros((T, N), bool); m = hi >= 0
    out[m] = (p[hi[m] + 1] - p[lo[m]]) > 0
    return out, sp
