import os as _os
#!/usr/bin/env python3
"""Round 7 (A): public-data impact calibration, download + reduce.  Only AGGREGATES per coin-day are kept; raw zips are streamed and discarded.
 bookDepth (UM, data.binance.vision): per sample day, median over 30s snapshots of the cumulative notional within +-0.2/1/2/3/4/5% of mid, bid and ask.
 aggTrades (UM): per sample day: (i) effective half-spread = median over 1-second buckets that contain both buy- and sell-aggressor trades of (vwap_buy - vwap_sell)/(2*mid);
                  (ii) square-root-law coefficient Y_kyle = OLS-through-origin slope of signed 5-minute return on  sigma_day*sign(Q)*sqrt(|Q|/ADV_day)  (Q = buy-minus-sell aggressor notional in the bucket).
Usage: python calib_dl.py bd|ag  (sample days: bookDepth every 14 days, aggTrades every 28 days from 2025-10-01)"""
import sys, io, zipfile, time, os, datetime as dt
import numpy as np, pandas as pd, requests
from multiprocessing import Pool
BASE = "https://data.binance.vision/data/futures/um/daily"
OUT = _os.environ.get("CALIB_DIR", "calib_private")
S = None
def sess():
    global S
    if S is None:
        S = requests.Session(); S.headers.update({"User-Agent": "calib-research/1.0"})
    return S
def get(url):
    for i in range(4):
        try:
            r = sess().get(url, timeout=120)
            if r.status_code == 404: return None
            if r.status_code == 200: return r.content
        except Exception: pass
        time.sleep(1 + 2 * i)
    return "ERR"
def do_bd(a):
    sym, d = a
    b = get(f"{BASE}/bookDepth/{sym}/{sym}-bookDepth-{d}.zip")
    if b is None or isinstance(b, str): return (sym, d, "NA" if b is None else "ERR", None)
    df = pd.read_csv(io.BytesIO(zipfile.ZipFile(io.BytesIO(b)).read(zipfile.ZipFile(io.BytesIO(b)).namelist()[0])))
    df = df[df.percentage.abs().isin([0.2, 1, 2, 3, 4, 5])]
    pv = df.pivot_table(index="timestamp", columns="percentage", values="notional", aggfunc="last")
    out = {}
    for L in (0.2, 1, 2, 3, 4, 5):
        if L in pv and -L in pv:
            out[f"bid{L}"] = float(pv[-L].median()); out[f"ask{L}"] = float(pv[L].median())
    out["n_snap"] = len(pv)
    return (sym, d, "OK", out)
def do_ag(a):
    sym, d = a
    b = get(f"{BASE}/aggTrades/{sym}/{sym}-aggTrades-{d}.zip")
    if b is None or isinstance(b, str): return (sym, d, "NA" if b is None else "ERR", None)
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(z.open(z.namelist()[0]), usecols=lambda c: c in ("price", "quantity", "transact_time", "is_buyer_maker"))
    if df.is_buyer_maker.dtype == object: df["is_buyer_maker"] = df.is_buyer_maker.astype(str).str.lower() == "true"
    t = df.transact_time.values; p = df.price.values.astype(float); q = df.quantity.values.astype(float); notl = p * q
    buy = ~df.is_buyer_maker.values                      # taker (aggressor) is the buyer
    sec = (t // 1000).astype(np.int64)
    g = pd.DataFrame({"s": sec, "n": notl, "b": buy, "pn": p * notl})
    g["bn"] = np.where(g.b, g.n, 0.0); g["sn"] = np.where(g.b, 0.0, g.n); g["bp"] = np.where(g.b, g.pn, 0.0); g["sp"] = np.where(g.b, 0.0, g.pn)
    a_ = g.groupby("s")[["bn", "sn", "bp", "sp"]].sum()
    a_ = a_[(a_.bn > 0) & (a_.sn > 0)]
    mid = float(np.median(p))
    hs = float(np.median((a_.bp / a_.bn - a_.sp / a_.sn) / (2.0 * (a_.bp / a_.bn + a_.sp / a_.sn) / 2.0))) if len(a_) > 50 else np.nan
    # 5-minute buckets
    bk = (t // 300000).astype(np.int64)
    h = pd.DataFrame({"k": bk, "p": p, "sg": np.where(buy, notl, -notl), "n": notl})
    ag = h.groupby("k").agg(p0=("p", "first"), p1=("p", "last"), Q=("sg", "sum"), V=("n", "sum"))
    ag = ag.reindex(range(int(ag.index.min()), int(ag.index.max()) + 1))
    r5 = np.log(ag.p1 / ag.p0).fillna(0.0).values
    sig_day = float(np.std(np.log(ag.p1 / ag.p1.shift(1)).dropna().values) * np.sqrt(288.0))
    Qb = ag.Q.fillna(0.0).values; adv = float(ag.V.fillna(0).sum())
    x = sig_day * np.sign(Qb) * np.sqrt(np.abs(Qb) / max(adv, 1.0)); m = np.isfinite(x) & (x != 0)
    y_k = float((x[m] * r5[m]).sum() / (x[m] ** 2).sum()) if m.sum() > 30 else np.nan
    # flow share of volume at 5-min: mean |Q|/V
    return (sym, d, "OK", dict(hs=hs, y_kyle=y_k, sig_day=sig_day, adv=adv, n_trd=len(df), n_sec=len(a_), mid=mid))
def dates(step):
    d0 = dt.date(2025, 10, 1); out = []
    while d0 <= dt.date(2026, 10, 21):
        out.append(d0.isoformat()); d0 += dt.timedelta(days=step)
    return out
if __name__ == "__main__":
    mode = sys.argv[1]
    coins = [c for c in open(sys.argv[2]).read().split() if c]
    step = 14 if mode == "bd" else 28
    fn = do_bd if mode == "bd" else do_ag
    fp = f"{OUT}/{mode}.csv"; done = set()
    if os.path.exists(fp):
        old = pd.read_csv(fp); done = set(zip(old.sym, old.date))
    jobs = [(s, d) for d in dates(step) for s in coins if (s, d) not in done]
    print(mode, "jobs", len(jobs), flush=True)
    rows = []; t0 = time.time(); na = err = 0
    with Pool(16) as pool:
        for i, (sym, d, st, o) in enumerate(pool.imap_unordered(fn, jobs, chunksize=2)):
            if st == "OK": rows.append(dict(sym=sym, date=d, **o))
            elif st == "NA": na += 1
            else: err += 1
            if (i + 1) % 200 == 0:
                print(i + 1, len(jobs), f"{time.time()-t0:.0f}s ok={len(rows)} na={na} err={err}", flush=True)
                pd.DataFrame(rows).to_csv(fp + ".part", index=False)
    new = pd.DataFrame(rows)
    if done: new = pd.concat([old, new])
    new.to_csv(fp, index=False); print("done", len(new), "na", na, "err", err, flush=True)
