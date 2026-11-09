#!/usr/bin/env python3
# NOTE: 本文件的独立命令行预设（OPS: pov=0.20、24h 成交量 10% 等）是早期 v10 口径，头条复现（repro/run_headline.py）不使用；头条参数由 lib12s.CFG_V10 / r9_run / r10_run 的 cons_c0 设定覆盖。
"""Pure negative-funding backtest: sell spot + hold a 1x perp long while funding is negative (Binance USDT-M).

EVERYTHING that changes the reported number lives in this file / engine.py - no post-processing:
  * funding is reported exactly as realised (no haircut);                              [engine: ev]
  * volume limit  : position <= vol_frac (10%) x trailing-24h volume known at t-1; enforced while holding
                    (excess is sold down subject to the normal execution limits); zero volume = no room   [engine, Data.dv]
  * OI limit      : position <= oi_frac (10%) x OI of the previous completed HOUR (hourly, causal); enforced while holding;
                    unknown OI = cannot hold                                                        [engine, Data.oilim]
  * inventory cap : per-symbol cap (cap_mode=holdings) = what you can actually sell; cap_mode=equal = S/#symbols
  * impact        : 20bp per fill per leg by default (10/30 are sensitivities)
  * basis filter  : none by default (the old fixed -38bp filter was a fitted constant; -20/-38/-60 are sensitivities)
  * idle cash yield is NOT in the headline; it is only printed as separate, labelled sensitivity columns (0/3/5%).

Reported per window (1y = headline, 6m): net U, annualised return on S AND on average utilised capital, PnL split
(funding / basis convergence / spread+impact / fees), quarterly figures, rolling 90-day figures, bootstrap 95% range.

Examples
  python run_backtest.py                                        # public, equal cap S/N, S=70M, both windows
  python run_backtest.py --cap-mode holdings --holdings-cap holdings_cap.csv   # your own inventory cap (see make_holdings_cap.py)
  python run_backtest.py --impact-bp 30 --window 1y --no-files
"""
import argparse, json, sys
import numpy as np, pandas as pd
import config as C
from engine import sim, mkp, PI

T0 = pd.Timestamp(C.DATA_START, tz="UTC")
T1 = pd.Timestamp(C.DATA_END, tz="UTC")


def hidx(ts):
    return int((pd.Timestamp(ts, tz="UTC") - T0).total_seconds() // 3600)


def years_of(window):
    a, b = C.WINDOWS[window]
    return (pd.Timestamp(b) - pd.Timestamp(a)).days / 365.0


# v8 fixes (all on = the headline). Set a flag to 0 to get the 20d9522 (v7) behaviour of that item; waterfall.py switches them on one at a time.
FIXES = dict(f_exitlag=1,    # exit/derisk decisions use the previous-hour close (fills at the hour VWAP)
             f_markfund=1,   # funding valued at the mark price at settlement time (markPriceKlines open of the settlement hour)
             f_coin=0,       # SENSITIVITY ONLY: token ledger for the inventory cap (A: coins = Borrowable Qty, B: S/N converted with the t-1 close). Headline keeps the v7 USDT cap
             f_budget=1,     # ONE participation budget per coin-hour shared by entry + add + exit + de-risk
             f_prior=1,      # that budget (and the depth proxy) is taken from the PRIOR hour (2 = reviewer's min(prior, current) variant, diagnostic)
             f_hardcap=1,    # 10% volume (spot notional) and 10% OI (PERP notional) are hard caps for entry/add and the de-risk target (2 = soft targets, OI on perp notional)
             f_exitpov=1,    # window-end / timeout exits obey participation; missing bar = defer; unwind starts wind_down_h before the end
             f_addonce=0,    # SENSITIVITY ONLY: one top-up per episode. Headline keeps the actual rule: unlimited re-adds while the entry condition holds and room exists
             f_exitstate=1,  # exit wait test uses the whole-episode PnL state after partial de-risk
             f_mtmcap=1,     # capital in use REPORTED at market value (2 = sensitivity: the capital limit itself uses market value)
             f_mtmderisk=0,  # SENSITIVITY ONLY (capacity_curve.py): enforce MTM usage <= S: derisk pro rata + no new entries/adds when MTM(t-1 close) >= S
             max_adds=1)
FIX_ORDER = ["f_exitlag", "f_markfund", "f_budget", "f_prior", "f_hardcap", "f_exitpov", "f_exitstate", "f_mtmcap"]
SENS_ORDER = [("f_coin", 1), ("f_addonce", 1), ("f_mtmcap", 2)]

# Execution preset (depth / participation limits); all others are engine defaults (fees spot 10bp / perp 5bp taker, no BNB discount).
# NOTE: 本文件的独立命令行预设（OPS: pov=0.20、24h 成交量 10% 等）是早期 v10 口径，头条复现（repro/run_headline.py）不使用；头条参数由 lib12s.CFG_V10 / r9_run / r10_run 的 cons_c0 设定覆盖。
OPS = dict(exec_mode=0, spread_mult=1.0, slip_mode=0, pov=0.20, xmax=0.02, dfrac=0.75, vol_frac=0.10,
           add_n=1, wait_max_n=168, wait_cap=0.003)
NO_BASIS = 1.0      # entry_basis_n: basis <= +100% i.e. no basis filter


class CapSpec:
    """per-coin inventory cap: usdt = legacy USDT cap (A: qty x window-end price; B: S/N); qty = token ledger (A only, PRIVATE; 0 = none)."""
    def __init__(self, usdt, qty=None):
        self.usdt = np.asarray(usdt, float); self.qty = np.zeros_like(self.usdt) if qty is None else np.asarray(qty, float)

    def __array__(self, dtype=None, copy=None):
        return self.usdt if dtype is None else self.usdt.astype(dtype)


class Data:
    KEYS = ["sv", "sc", "sq", "fv", "fc", "fq", "ev", "r8", "r8s", "d02", "d1"]

    def __init__(self, ddir, name="dataset"):
        z = np.load(ddir / f"{name}.npz")
        self.meta = pd.read_csv(ddir / f"{name}_meta.csv")
        self.arr = [z[k].astype(np.float64) for k in self.KEYS]
        self.oi = z["oi"].astype(np.float64)                      # previous-UTC-day OI (v4, legacy), NaN = unknown
        self.oih = z["oih"].astype(np.float64)                    # OI of the previous completed hour, NaN = unknown
        self.mk = np.nan_to_num(z["mk"].astype(np.float64)) if "mk" in z.files else None   # mark price at hour start (spot-equivalent), 0 = missing
        self.fut = self.meta.fut.tolist(); self.N = len(self.fut)
        self.spot = self.meta.spot.tolist()
        self.hs_s = self.meta.hs_s.to_numpy(float); self.hs_f = self.meta.hs_f.to_numpy(float)
        self.r2 = self.meta.r2.to_numpy(float); self.r5 = self.meta.r5.to_numpy(float)
        self.ddir = ddir
        sq, fq = self.arr[2], self.arr[5]; T = sq.shape[0]
        # causal volume basis: trailing 24h quote volume over hours t-24 .. t-1, min(spot, perp)
        def trail(x):
            c = np.concatenate([np.zeros((1, x.shape[1])), np.cumsum(np.nan_to_num(x), axis=0)])
            out = np.zeros_like(x); out[24:] = c[24:T] - c[:T - 24]
            return out
        self.dv = np.minimum(trail(sq), trail(fq))
        self._sameday = None; self._oi_static = None

    # ---- legacy (non-causal) quantities, ONLY used by run_sensitivity.py to price the fixes ----
    def dv_sameday(self):
        """old rule: full UTC-day volume (incl. future hours) broadcast to the day; dv==0 bypassed the cap."""
        if self._sameday is None:
            sq, fq = self.arr[2], self.arr[5]; T = sq.shape[0]; D = T // 24
            sday = np.nan_to_num(sq[:D * 24]).reshape(D, 24, -1).sum(1); fday = np.nan_to_num(fq[:D * 24]).reshape(D, 24, -1).sum(1)
            dv = np.zeros_like(sq); dv[:D * 24] = np.repeat(np.minimum(sday, fday), 24, axis=0)
            dv[dv <= 0] = 1e300
            self._sameday = dv
        return self._sameday

    def oi_static(self):
        """old rule: full-sample median daily OI of the 1y window (uses future data)."""
        if self._oi_static is None:
            a, b = C.WINDOWS["1y"]; med = np.full(self.N, np.nan)
            for i, f in enumerate(self.fut):
                p = self.ddir / "oi" / f"{f}.csv"
                if p.exists():
                    d = pd.read_csv(p); d = d[(d.date >= a) & (d.date < b)]
                    if d.oi_usdt.notna().any(): med[i] = np.nanmedian(d.oi_usdt)
            self._oi_static = med
        return self._oi_static

    def oiv_for(self, oi_mode="hour", oi_extra_lag_h=0):
        oi = self.oih if oi_mode != "lag" else self.oi
        if oi_extra_lag_h: oi = np.vstack([np.full((oi_extra_lag_h, oi.shape[1]), np.nan), oi[:-oi_extra_lag_h]])
        return np.where(np.isfinite(oi), oi, 0.0)

    def sigma(self):
        """daily volatility proxy known at t-1 (only used by the sqrt-impact sensitivity): std of the 24 hourly spot log-returns of hours t-24..t-1 x sqrt(24);
        needs >= 12 returns, hours without enough history use the pool median of that hour... simplified: the global median of all valid values."""
        if getattr(self, "_sg", None) is None:
            sc = pd.DataFrame(self.arr[1]).where(lambda x: x > 0); lr = np.log(sc).diff()
            sd = lr.rolling(24, min_periods=12).std().shift(1).to_numpy() * np.sqrt(24.0)
            med = np.nanmedian(sd); self._sg = np.where(np.isfinite(sd), sd, med)
        return self._sg

    def causal_min_arrays(self):
        """reviewer's P2-04a2 variant of 'prior-hour' depth/volume: element-wise min(prior hour, current hour) of sq, fq, d02, d1 (needs the current hour: NOT strictly causal)."""
        if getattr(self, "_cm", None) is None:
            arr = list(self.arr)
            for k in (2, 5, 9, 10):
                x = arr[k]; p = np.vstack([x[:1], x[:-1]]); xp = np.where(p > 0, p, x); xc = np.where(x > 0, x, p)
                arr[k] = np.minimum(xp, xc)
            self._cm = arr
        return self._cm

    def streak_arr(self, thr=0.001):
        """v10: number of CONSECUTIVE funding prints (settlements) up to hour t whose 8h-scaled rate is below -thr (= entry-grade). Reset to 0 by any print that is not. Causal."""
        key = round(float(thr), 8)
        if getattr(self, "_stk", None) is None: self._stk = {}
        if key not in self._stk:
            ev, r8 = self.arr[6], self.arr[7]; T, N = r8.shape
            out = np.zeros((T, N)); cnt = np.zeros(N)
            prt = (ev != 0)
            qual = np.nan_to_num(r8, nan=0.0) < -thr
            for t in range(T):
                p = prt[t]
                cnt = np.where(p, np.where(qual[t], cnt + 1, 0), cnt)
                out[t] = cnt
            self._stk[key] = out
        return self._stk[key]

    def ivh(self):
        """funding interval (hours) of the event settled in hour t: ev*8/r8; 0 where no event"""
        if getattr(self, "_iv", None) is None:
            ev, r8 = self.arr[6], self.arr[7]
            with np.errstate(all="ignore"): q = np.where((ev != 0) & (np.abs(r8) > 0), ev * 8.0 / r8, 0.0)
            self._iv = np.clip(np.round(np.nan_to_num(q)), 0, 8)
        return self._iv

    def run(self, window, S, ok, cap_vec, params, oi_frac=0.10, oi_mode="hour", dv=None, oi_extra_lag_h=0, ok_m=None, cap_m=None):
        """oi_mode: hour (previous completed hour OI, enforced while holding; default) | lag (v4: previous-UTC-day OI) | static (legacy, via cap_vec) | off"""
        a, b = C.WINDOWS[window]; h0, h1 = hidx(a), hidx(b)
        cap_q = np.zeros(self.N)
        if isinstance(cap_vec, CapSpec): cap_q = np.where(ok > 0, cap_vec.qty, 0.0); cap_vec = cap_vec.usdt
        params = dict(params); arr = self.arr; stk = self.streak_arr(params.pop('streak_thr', 0.001))
        if params.get("f_prior", 0) == 2: arr = self.causal_min_arrays(); params["f_prior"] = 0
        P = mkp(capital=S, **params)
        if params.get("f_markfund", 0) and self.mk is None: raise SystemExit("dataset has no mark prices: run download_markprice.py and build_dataset.py")
        mk = self.mk if self.mk is not None else np.zeros_like(self.arr[0])
        T = self.arr[0].shape[0]
        if oi_frac and oi_mode in ("hour", "lag"):
            oi = self.oih if oi_mode == "hour" else self.oi
            if oi_extra_lag_h:                                    # diagnostic only: make the hourly OI staler by k more hours
                oi = np.vstack([np.full((oi_extra_lag_h, oi.shape[1]), np.nan), oi[:-oi_extra_lag_h]])
            oilim = np.where(np.isfinite(oi), oi_frac * oi, 0.0)
        else:
            oilim = np.full((T, self.N), 1e300)
        cap = np.where(ok > 0, cap_vec, 0.0)
        if oi_frac and oi_mode == "static":
            m = self.oi_static(); cap = np.where(np.isfinite(m), np.minimum(cap, oi_frac * m), cap)
        okm = np.zeros((T, self.N), dtype=np.uint8)
        if ok_m is not None: okm[:] = (np.asarray(ok_m) > 0).astype(np.uint8)[None, :]
        capm = np.zeros(self.N) if cap_m is None else np.asarray(cap_m, float)
        H = h1 - h0
        used = np.zeros(H); cum = np.zeros(H); comp = np.zeros((H, 6)); epb = np.zeros((200000, 8)); diag = np.zeros(24)
        res, blocked, lost_f = sim(h0, h1, *arr, self.dv if dv is None else dv, oilim, self.hs_s, self.hs_f, self.r2, self.r5,
                           ok, okm, cap, capm, mk, cap_q, self.oiv_for(oi_mode, oi_extra_lag_h), self.ivh(), self.sigma(), stk, P, used, cum, epb, comp, diag)
        price, fund, fees, cost, traded, nf, ne, ar = res.sum(axis=0)   # v10: row0 = negative leg, row1 = positive (mirror) leg
        return dict(window=window, S=S, price=price, funding=fund, fees=fees, cost=cost, traded=traded,
                    impact=(float("nan") if params.get("sqrt_y", 0) > 0 else 2.0 * params.get("impact", 0.0) * traded), episodes=int(ne), fills=int(nf), lost_f=lost_f, diag=diag, used=used, cum=cum, comp=comp,
                    ep=epb[: int(min(ne, len(epb)))], years=years_of(window))


def metrics(r, hair=1.0):
    """Headline metrics. hair=1 = raw realised funding (the only value used in the repo's reports)."""
    S, y = r["S"], r["years"]
    net = r["price"] + hair * r["funding"] - r["fees"]
    au = float(r["used"].mean())
    m = dict(window=r["window"], S_wan=S / 1e4, years=round(y, 4), net_wan=net / 1e4,
             funding_wan=hair * r["funding"] / 1e4, basis_gross_wan=(r["price"] + r["cost"]) / 1e4,
             spread_impact_wan=-r["cost"] / 1e4, of_which_impact_wan=-r["impact"] / 1e4, fees_wan=-r["fees"] / 1e4,
             ann_on_S_pct=net / S / y * 100, avg_used_wan=au / 1e4, avg_used_pct_of_S=au / S * 100,
             max_used_wan=float(r["used"].max()) / 1e4,
             ann_on_used_pct=(net / au / y * 100) if au > 0 else float("nan"), episodes=r["episodes"])
    d = r.get("diag")
    if d is not None:
        Hh = len(r["used"])
        m.update(avg_used_cost_wan=d[15] / Hh / 1e4, avg_used_mtm_wan=d[14] / Hh / 1e4, peak_used_cost_wan=d[12] / 1e4, peak_used_mtm_wan=d[13] / 1e4,
                 peak_used_mtm_pct_of_S=d[13] / S * 100)
        m.update(fills=r["fills"], lost_funding_wan=r.get("lost_f", 0.0) / 1e4, residual_end_wan=d[8] / 1e4, residual_liq_cost_wan=d[9] / 1e4,
                 hours_over_vol_cap=int(d[0]), hours_over_oi_cap=int(d[1]), max_vol_cap_ratio=d[2], max_oi_cap_ratio=d[3],
                 fills_over_prior_pov=int(d[4]), max_prior_pov_pct=d[5] * 100, fills_over_same_hour_pov=int(d[6]), max_same_hour_pov_pct=d[7] * 100,
                 coin_hours_held=int(d[11]), funding_events_no_mark=int(d[10]),
                 derisk_traded_wan=d[16] / 1e4, derisk_fee_cost_wan=d[17] / 1e4, hours_mtm_over_S=int(d[18]), hours_derisk_active=int(d[19]), hours_trim_short=int(d[20]), max_trim_short_ratio=d[21])
    for rate in (0.0, 0.03, 0.05):          # SENSITIVITY ONLY: idle cash yield on (S - avg used); no evidence for any value
        idle = rate * max(S - au, 0.0) * y
        m[f"sens_idle{int(rate*100)}_ann_on_S_pct"] = (net + idle) / S / y * 100
    return m


def coin_pnl(r, N, net=True):
    """per-coin episode PnL (price + funding; net=True also subtracts every fee paid in the episode). Ranking / concentration use the NET figure."""
    ep = r["ep"]
    if not len(ep): return np.zeros(N)
    w = ep[:, 3] - (ep[:, 6] if net else 0.0)
    return np.bincount(ep[:, 0].astype(int), weights=w, minlength=N)


def concentration(r, N):
    """share of NET (after fees) episode PnL earned by the top-k coins (ranked by net PnL); *_gross = old before-fee definition. Aggregate only."""
    ep = r["ep"]; out = {}
    out["coins_traded"] = int((np.bincount(ep[:, 0].astype(int), minlength=N) > 0).sum()) if len(ep) else 0
    for net, sfx in ((True, ""), (False, "_gross")):
        pnl = coin_pnl(r, N, net); tot = pnl.sum(); srt = np.sort(pnl)[::-1]
        out[f"coins_profitable{sfx}"] = int((pnl > 0).sum())
        for k in (1, 5, 10): out[f"top{k}_share{sfx}_pct"] = srt[:k].sum() / tot * 100
    return out


def blocks(window):
    n = max(1, int(round(((pd.Timestamp(C.WINDOWS[window][1]) - pd.Timestamp(C.WINDOWS[window][0])).days) / 91.25)))
    H = hidx(C.WINDOWS[window][1]) - hidx(C.WINDOWS[window][0])
    edges = np.linspace(0, H, n + 1).round().astype(int)
    return edges


def quarterly(r):
    """Equal-length blocks (~91 days) of the window: 4 for 1y, 2 for 6m. cum = cumulative net incl. mark-to-market of open positions."""
    a, _ = C.WINDOWS[r["window"]]; S = r["S"]; e = blocks(r["window"]); rows = []
    cum = np.concatenate([[0.0], r["cum"]]); comp = np.vstack([np.zeros((1, 6)), r["comp"]])
    for i in range(len(e) - 1):
        lo, hi = e[i], e[i + 1]; yrs = (hi - lo) / 8760.0
        d = comp[hi] - comp[lo]; net = cum[hi] - cum[lo]; au = r["used"][lo:hi].mean()
        rows.append(dict(window=r["window"], block=f"Q{i+1}", start=str((pd.Timestamp(a, tz="UTC") + pd.Timedelta(hours=int(lo))).date()),
                         end=str((pd.Timestamp(a, tz="UTC") + pd.Timedelta(hours=int(hi))).date()), net_wan=net / 1e4,
                         funding_wan=d[0] / 1e4, basis_gross_wan=(d[1] + d[3]) / 1e4, spread_impact_wan=-d[3] / 1e4, fees_wan=-d[2] / 1e4,
                         mtm_change_wan=(comp[hi, 5] - comp[lo, 5]) / 1e4,
                         ann_on_S_pct=net / S / yrs * 100, avg_used_wan=au / 1e4,
                         ann_on_used_pct=(net / au / yrs * 100) if au > 0 else float("nan")))
    return pd.DataFrame(rows)


def rolling(r, days=90, step_days=7):
    S = r["S"]; cum = np.concatenate([[0.0], r["cum"]]); a, _ = C.WINDOWS[r["window"]]; rows = []
    W = days * 24
    for hi in range(W, len(cum), step_days * 24):
        lo = hi - W; net = cum[hi] - cum[lo]; au = r["used"][lo:hi].mean()
        rows.append(dict(window=r["window"], end=str((pd.Timestamp(a, tz="UTC") + pd.Timedelta(hours=hi)).date()),
                         ann_on_S_pct=net / S / (W / 8760.0) * 100,
                         ann_on_used_pct=(net / au / (W / 8760.0) * 100) if au > 0 else float("nan")))
    return pd.DataFrame(rows)


def bootstrap(r, n=5000, seed=0):
    """Resample whole weeks (with replacement) of hourly net PnL; annualise the mean week. Captures within-sample week-to-week
    noise only - it cannot see regime change (see quarterly/rolling), and it is not a forecast interval."""
    S = r["S"]; cum = np.concatenate([[0.0], r["cum"]]); H = len(cum) - 1; wk = np.arange(0, H, 168)
    wp = np.diff(np.concatenate([cum[wk], [cum[-1]]]))
    wlen = np.diff(np.concatenate([wk, [H]])) / 168.0
    rng = np.random.default_rng(seed); k = len(wp); idx = rng.integers(0, k, (n, k))
    ann = wp[idx].sum(1) / wlen[idx].sum(1) * (365.0 / 7.0)
    au = float(r["used"].mean())
    lo, md, hi = np.percentile(ann, [2.5, 50, 97.5])
    return dict(window=r["window"], weeks=k, boot_ann_on_S_lo_pct=lo / S * 100, boot_ann_on_S_mid_pct=md / S * 100, boot_ann_on_S_hi_pct=hi / S * 100,
                boot_ann_on_used_lo_pct=lo / au * 100, boot_ann_on_used_hi_pct=hi / au * 100, weekly_pnl_positive_share_pct=float((wp > 0).mean() * 100))


def load_ok(D, exclude=""):
    excl = {s for s in exclude.split(",") if s}
    return np.array([0.0 if f in excl else 1.0 for f in D.fut])


def make_cap(D, ok, S, cap_mode, holdings_cap=None):
    n_ok = int(ok.sum())
    if cap_mode == "equal":
        return CapSpec(np.where(ok > 0, S / max(n_ok, 1), 0.0))
    if not holdings_cap: sys.exit("--cap-mode holdings needs --holdings-cap CSV (fut_symbol,cap_usdt); build it with make_holdings_cap.py")
    h = pd.read_csv(holdings_cap); hm = dict(zip(h.fut_symbol, h.cap_usdt.astype(float)))
    miss = [f for f, o in zip(D.fut, ok) if o > 0 and f not in hm]
    if miss: print(f"note: {len(miss)} symbols absent from the cap file get cap 0", file=sys.stderr)
    usdt = np.where(ok > 0, np.minimum(np.array([hm.get(f, 0.0) for f in D.fut]), S), 0.0)
    if "qty" in h.columns: qm = dict(zip(h.fut_symbol, h.qty.astype(float)))                  # token ledger written by make_holdings_cap.py (PRIVATE)
    else:                                                                                  # older private file: qty = cap_usdt / the same window-end close
        qm = {}
        for f, sp in zip(D.fut, D.spot):
            if f in hm:
                d = pd.read_csv(D.ddir / "spot_klines" / f"{sp}.csv", usecols=["open_time", "close"]); d = d[d.open_time < int(T1.value // 10**6)]
                qm[f] = hm[f] / float(d.sort_values("open_time").close.iloc[-1])
    qty = np.where(ok > 0, np.array([qm.get(f, 0.0) for f in D.fut]), 0.0)
    return CapSpec(usdt, qty)


def base_params(impact_bp=20.0, basis_bp=None, legacy=False, **kw):
    """legacy=True -> 20d9522 (v7) behaviour: every f_* flag off (only run_sensitivity's historical bridge and waterfall.py use it)."""
    p = dict(OPS); p.update({k: (0 if legacy and k.startswith("f_") else v) for k, v in FIXES.items()}); p["impact"] = impact_bp / 1e4
    p["entry_basis_n"] = NO_BASIS if basis_bp is None else basis_bp / 1e4
    p.update(kw)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=None); ap.add_argument("--dataset", default="dataset")
    ap.add_argument("--window", choices=["1y", "6m", "both"], default="both")
    ap.add_argument("--S", type=float, default=7e7, help="notional capital S in USDT (default 70M)")
    ap.add_argument("--cap-mode", choices=["equal", "holdings"], default="equal")
    ap.add_argument("--holdings-cap", default=None)
    ap.add_argument("--impact-bp", type=float, default=20.0)
    ap.add_argument("--entry-basis-bp", type=float, default=None, help="optional basis filter, e.g. -38 (default: none)")
    ap.add_argument("--vol-frac", type=float, default=0.10, help="position <= this x trailing-24h volume (t-1); 0 = off")
    ap.add_argument("--oi-frac", type=float, default=0.10, help="position <= this x OI of the previous completed hour; 0 = off")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--tag", default=None, help="output file tag (default: cap mode)")
    ap.add_argument("--out-dir", default=None); ap.add_argument("--no-files", action="store_true")
    a = ap.parse_args()

    D = Data(C.data_dir(a.data_dir), a.dataset)
    ok = load_ok(D, a.exclude)
    cap = make_cap(D, ok, a.S, a.cap_mode, a.holdings_cap)
    params = base_params(a.impact_bp, a.entry_basis_bp, vol_frac=a.vol_frac)
    wins = ["1y", "6m"] if a.window == "both" else [a.window]
    H, Q, R, B = [], [], [], []
    for w in wins:
        r = D.run(w, a.S, ok, cap, params, oi_frac=a.oi_frac)
        m = metrics(r); m.update(bootstrap(r)); m.update(concentration(r, D.N)); m.pop("window", None); m = dict(window=w, **m)
        m.update(cap_mode=a.cap_mode, n_symbols=int(ok.sum()), impact_bp=a.impact_bp, vol_frac=a.vol_frac, oi_frac=a.oi_frac,
                 entry_basis_bp=a.entry_basis_bp)
        H.append(m); Q.append(quarterly(r)); R.append(rolling(r)); B.append(r)
        print(f"[{w}] net {m['net_wan']:.1f}万  ann on S {m['ann_on_S_pct']:.2f}%  ann on utilised {m['ann_on_used_pct']:.1f}%  "
              f"(avg used {m['avg_used_wan']:.0f}万 = {m['avg_used_pct_of_S']:.1f}% of S)  boot95 [{m['boot_ann_on_S_lo_pct']:.2f}, {m['boot_ann_on_S_hi_pct']:.2f}]%  "
              f"coins {m['coins_traded']} top5 {m['top5_share_pct']:.0f}%  funding {m['funding_wan']:.0f} basis {m['basis_gross_wan']:.0f} spr+imp {m['spread_impact_wan']:.0f} fees {m['fees_wan']:.0f}")
    H, Q, R = pd.DataFrame(H), pd.concat(Q), pd.concat(R)
    if not a.no_files:
        od = __import__("pathlib").Path(a.out_dir or C.REPO / "out"); od.mkdir(exist_ok=True)
        tag = a.tag or a.cap_mode
        H.to_csv(od / f"headline_{tag}.csv", index=False); Q.to_csv(od / f"quarterly_{tag}.csv", index=False)
        R.to_csv(od / f"rolling90d_{tag}.csv", index=False)
        print("->", od / f"headline_{tag}.csv")
    print(Q.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
