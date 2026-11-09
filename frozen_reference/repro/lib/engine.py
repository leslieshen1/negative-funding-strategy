#!/usr/bin/env python3
"""
Hourly, time-synchronous portfolio backtest engine (numba) over N coins.

Side s=+1: sell spot / long 1x perp   -> negative-funding harvest ("neg leg", the only leg used by this repo)
Side s=-1: long spot / short perp     -> positive-funding carry ("pos leg", disabled: mirror_on=0)
PnL = pair residual (spot-leg price pnl + perp-leg price pnl) + funding - fees, in USDT.
The price move of the underlying inventory itself is NOT included (market-to-market excluded).

Limits (all enforced here, nothing is applied after the run):
  dv[t,n]    causal volume basis (trailing 24h up to t-1, built by the caller); room = vol_frac*dv - current notional; dv==0 -> no room
  oilim[t,n] causal OI limit in USDT (e.g. 10% x OI of the previous completed hour; 0 = unknown -> cannot hold)
  derisk=1   while holding, if position notional > min(vol_frac*dv, oilim)*(1+derisk_tol) the excess is sold down, subject to the
             normal per-hour execution limits (pov / depth), so brief overshoots can remain
  entry_basis_n: entry only if (perp-spot)/spot <= this; 1.0 = no basis filter (default)
"""
import numpy as np, pandas as pd
from numba import njit

PN = ["entry_fav_n", "entry_basis_n", "exit_fav_n", "wait_max_n", "wait_cap",
      "mirror_on", "entry_fav_m", "entry_basis_m", "exit_fav_m", "wait_max_m", "mirror_spot_trade", "margin_m",
      "pov", "xmax", "dfrac", "rho", "slip_mode", "impact",
      "fee_s", "fee_f", "fee_s_mk", "fee_f_mk", "mk_share", "spread_mult",
      "add_n", "add_m", "max_hold_h", "exit_force_h", "wind_down_h", "capital", "min_u", "pos_cap_frac",
      "pov_m", "xmax_m", "dfrac_m", "exec_mode", "vol_frac", "derisk", "derisk_tol",
      # v8 fixes (0 = legacy 20d9522 behaviour, 1 = fixed); k_imp = own-impact sensitivity
      "f_exitlag", "f_markfund", "f_coin", "f_budget", "f_prior", "f_hardcap", "f_exitpov", "f_addonce", "f_exitstate", "f_mtmcap",
      "max_adds", "k_imp", "sqrt_y", "f_mtmderisk",
      # v10 (all default OFF = v9.1 behaviour)
      "derisk_force", "n_streak", "big_thr"]
PI = {k: i for i, k in enumerate(PN)}
DEFAULT = dict(entry_fav_n=0.001, entry_basis_n=1.0, exit_fav_n=0.0, wait_max_n=24, wait_cap=0.001,
               mirror_on=0, entry_fav_m=0.00012, entry_basis_m=-1.0, exit_fav_m=0.00005, wait_max_m=0, mirror_spot_trade=1, margin_m=1.0,
               pov=0.05, xmax=0.002, dfrac=0.5, rho=1.0, slip_mode=0, impact=0.0002,
               fee_s=0.001, fee_f=0.0005, fee_s_mk=0.001, fee_f_mk=0.0002, mk_share=0.0, spread_mult=1.0,
               add_n=0, add_m=1, max_hold_h=0, exit_force_h=336, wind_down_h=24, capital=3e6, min_u=0.0, pos_cap_frac=1.0,
               pov_m=0.05, xmax_m=0.01, dfrac_m=0.5, exec_mode=0, vol_frac=0.0, derisk=1, derisk_tol=0.0,
               f_exitlag=0, f_markfund=0, f_coin=0, f_budget=0, f_prior=0, f_hardcap=0, f_exitpov=0, f_addonce=0, f_exitstate=0, f_mtmcap=0,
               max_adds=1, k_imp=0.0, sqrt_y=0.0, f_mtmderisk=0,
               derisk_force=0, n_streak=1, big_thr=0.0)

def mkp(**kw):
    d = dict(DEFAULT); d.update(kw)
    bad = set(kw) - set(PN)
    assert not bad, bad
    return np.array([d[k] for k in PN], dtype=np.float64)

@njit(cache=True)
def knots(d02, d1, r2, r5, xs, ds):
    if not (d1 > 0): d1 = 0.0
    if not (d02 > 0): d02 = 0.0
    if d02 > 0.95 * d1: d02 = 0.95 * d1
    xs[0] = 0.0; ds[0] = 0.0
    xs[1] = 0.002; ds[1] = d02
    xs[2] = 0.01; ds[2] = d1
    xs[3] = 0.02; ds[3] = d1 * max(r2, 1.05)
    xs[4] = 0.05; ds[4] = d1 * max(r5, r2 * 1.05, 1.1)

@njit(cache=True)
def D_at(x, xs, ds):
    if x <= 0: return 0.0
    for k in range(4):
        if x <= xs[k + 1]:
            return ds[k] + (ds[k + 1] - ds[k]) * (x - xs[k]) / (xs[k + 1] - xs[k])
    return ds[4]

@njit(cache=True)
def avg_slip(Q, scale, xs, ds):
    if Q <= 0 or scale <= 0: return 0.0
    tot = 0.0
    done = False
    for k in range(4):
        d0 = ds[k] * scale; d1_ = ds[k + 1] * scale
        if d1_ <= d0: continue
        if Q <= d0: done = True; break
        qu = min(Q, d1_)
        slope = (xs[k + 1] - xs[k]) / (d1_ - d0)
        dq = qu - d0
        tot += dq * xs[k] + 0.5 * slope * dq * dq
        if Q <= d1_: done = True; break
    if not done and Q > ds[4] * scale:
        tot += (Q - ds[4] * scale) * xs[4]
    return tot / Q

@njit(cache=True)
def budget(tt, n, exec_mode, pv, xm, df, rho, spot_trade, sq, fq, d02, d1, r2, r5, xs, ds):
    """Participation budget (USDT notional per hour per coin) from the volume/depth known at hour index tt."""
    if exec_mode > 1.5: return 1e300
    a = sq[tt, n]; b = fq[tt, n]
    if not (a > 0): a = 0.0
    if not (b > 0): b = 0.0
    cv = pv * (min(a, b) if spot_trade else b)
    if exec_mode > 0.5: return cv
    knots(d02[tt, n], d1[tt, n], r2[n], r5[n], xs, ds)
    cd = df * D_at(xm, xs, ds) * (min(1.0, rho) if spot_trade else 1.0)
    return min(cv, cd)

@njit(cache=True)
def sim(t0, t1, sv, sc, sq, fv, fc, fq, ev, r8, r8s, d02, d1, dv, oilim, hs_s, hs_f, r2, r5, ok_n, ok_m, cap_n, cap_m,
        mk, cap_q, oiv, ivh, sg, streak, P, out_used, out_cum, ep_buf, out_comp, diag):
    """diag (float64[24]): 0 coin-hours over the vol cap at hour end (spot VWAP notional > vol_frac*dv) | 1 coin-hours over the OI cap (PERP VWAP notional)
    2 max vol-cap ratio | 3 max OI ratio | 4 coin-hours with fills > pov x prior-hour min volume | 5 max fill/prior-hour volume
    6 coin-hours with fills > pov x same-hour volume | 7 max fill/same-hour volume | 8 residual notional at window end (liquidated by accounting)
    9 fees+costs charged on that residual liquidation | 10 funding events without a mark price (fallback used) | 11 coin-hours held
    12 peak capital in use at COST basis | 13 peak at MARK-TO-MARKET (t close) | 14 sum over hours of MTM use | 15 sum over hours of cost-basis use | (f_mtmderisk only) 16 notional sold by the MTM de-risk | 17 fees+spread/impact paid on those fills | 18 hours with MTM use at the hour close > S | 19 hours with an active trim | 20 hours with use at the t-1 closes still > S after the trim (participation-limited) | 21 max such ratio"""
    TT, N = sv.shape
    entry_fav_n = P[0]; entry_basis_n = P[1]; exit_fav_n = P[2]; wait_max_n = P[3]; wait_cap = P[4]
    mirror_on = P[5]; entry_fav_m = P[6]; entry_basis_m = P[7]; exit_fav_m = P[8]; wait_max_m = P[9]
    mirror_spot_trade = P[10]; margin_m = P[11]; pov = P[12]; xmax = P[13]; dfrac = P[14]; rho = P[15]
    slip_mode = P[16]; impact = P[17]; fee_s = P[18]; fee_f = P[19]; fee_s_mk = P[20]; fee_f_mk = P[21]
    mk_share = P[22]; spread_mult = P[23]; add_n = P[24]; add_m = P[25]; max_hold_h = P[26]; exit_force_h = P[27]
    wind_down_h = P[28]; capital = P[29]; min_u = P[30]; pos_cap_frac = P[31]; pov_m = P[32]; xmax_m = P[33]; dfrac_m = P[34]; exec_mode = P[35]; vol_frac = P[36]; derisk = P[37]; derisk_tol = P[38]
    f_exitlag = P[39]; f_markfund = P[40]; f_coin = P[41]; f_budget = P[42]; f_prior = P[43]; f_hardcap = P[44]; f_exitpov = P[45]
    f_addonce = P[46]; f_exitstate = P[47]; f_mtmcap = P[48]; max_adds = P[49]; k_imp = P[50]; sqrt_y = P[51]; f_mtmderisk = P[52]
    derisk_force = P[53]; n_streak = P[54]; big_thr = P[55]
    lost_f = 0.0
    pos = np.zeros(N); side = np.zeros(N); sp_avg = np.zeros(N); fp_avg = np.zeros(N)
    fees_acc = np.zeros(N); fund_acc = np.zeros(N); used_n = np.zeros(N); capf = np.zeros(N); ent_notional = np.zeros(N)
    wait = np.zeros(N); wait_h = np.zeros(N); wait_paid = np.zeros(N); exiting = np.zeros(N)
    ep_start = np.zeros(N); exit_start = np.zeros(N); ep_max_u = np.zeros(N); ep_price = np.zeros(N)
    fees_ep = np.zeros(N); n_adds = np.zeros(N); nfill_ep = np.zeros(N); bud_used = np.zeros(N)
    last_sc = np.zeros(N); last_fc = np.zeros(N)
    for n in range(N):                       # last known closes before the first simulated hour (t0-1 and earlier)
        for tb in range(t0 - 1, -1, -1):
            if last_sc[n] <= 0 and sc[tb, n] > 0: last_sc[n] = sc[tb, n]
            if last_fc[n] <= 0 and fc[tb, n] > 0: last_fc[n] = fc[tb, n]
            if last_sc[n] > 0 and last_fc[n] > 0: break
    xs = np.zeros(5); ds = np.zeros(5)
    used_cap = 0.0; used_pos = 0.0
    tp = np.zeros(2); tf = np.zeros(2); tfe = np.zeros(2); tc = np.zeros(2); tu = np.zeros(2)
    nfill = np.zeros(2); nep = np.zeros(2); area = np.zeros(2)
    blocked = 0.0
    n_ep = 0
    cand_n = np.zeros(N, dtype=np.int64); cand_f = np.zeros(N); cand_s = np.zeros(N)
    for t in range(t0, t1):
        last_hr = (t == t1 - 1)
        tb_ = t - 1 if (f_prior > 0.5 and t > 0) else t          # hour index of the volume / depth known when the decision is taken
        for n in range(N):
            bud_used[n] = 0.0
            if f_exitlag < 0.5:                                     # legacy: decisions see the CURRENT hour close (look-ahead)
                if sc[t, n] > 0: last_sc[n] = sc[t, n]
                if fc[t, n] > 0: last_fc[n] = fc[t, n]
        mtm_f = 0.0                                                 # (f_mtmderisk, non-default) pro-rata trim fraction so that MTM usage at the t-1 closes <= capital
        if f_mtmderisk > 0.5:
            um0 = 0.0
            for n in range(N):
                if pos[n] > 0 and last_sc[n] > 0: um0 += pos[n] * last_sc[n] * capf[n]
            if um0 > capital:
                mtm_f = (um0 - capital) / um0
        # ---------- pass 1: open positions
        for n in range(N):
            if pos[n] <= 0: continue
            s = side[n]; li = 0 if s > 0 else 1
            e = ev[t, n]
            d8 = 0.0
            if k_imp > 0.0 and s > 0:                               # own-impact sensitivity: funding rises by k bp (8h) per 1% of OI held
                o_ = oiv[t, n]; pf_ = fc[t - 1, n] if (t > 0 and fc[t - 1, n] > 0) else last_fc[n]
                if o_ > 0 and pf_ > 0: d8 = k_imp * 1e-4 * (pos[n] * pf_ / o_) * 100.0
            if e != 0.0:
                if f_markfund > 0.5:
                    mk_ = mk[t, n]
                    if not (mk_ > 0):
                        diag[10] += 1.0
                        mk_ = last_fc[n] if last_fc[n] > 0 else last_sc[n]
                else:
                    mk_ = fc[t, n] if fc[t, n] > 0 else (last_fc[n] if last_fc[n] > 0 else last_sc[n])   # legacy: close of the settlement hour
                if e < 0.0 and d8 > 0.0:
                    iv_ = ivh[t, n] if ivh[t, n] > 0 else 8.0
                    e2 = min(e + d8 * iv_ / 8.0, 0.0); lost_f += (e2 - e) * mk_ * pos[n]; e = e2
                fnd = -s * e * mk_ * pos[n]
                fund_acc[n] += fnd; tf[li] += fnd
                if wait[n] > 0 and fnd < 0: wait_paid[n] += -fnd
            have_bar = (sv[t, n] > 0) and (fv[t, n] > 0)
            if (not have_bar) and ((not last_hr) or f_exitpov > 0.5): continue        # missing bar: defer (fixed) / stale-price close in the last hour (legacy)
            spot_mid = sv[t, n] if sv[t, n] > 0 else last_sc[n]
            fut_mid = fv[t, n] if fv[t, n] > 0 else last_fc[n]
            do_exit = False
            if exiting[n] > 0:
                do_exit = True
            else:
                r = r8[t, n] if s > 0 else r8s[t, n]
                if d8 > 0.0 and np.isfinite(r) and r < 0.0: r = min(r + d8, 0.0)
                if np.isfinite(r):
                    fav = -s * r
                    ex_thr = exit_fav_n if s > 0 else exit_fav_m
                    wmax = wait_max_n if s > 0 else wait_max_m
                    if fav < ex_thr:
                        if wmax <= 0:
                            do_exit = True
                        else:
                            hs_sx = hs_s[n] * spread_mult + impact; hs_fx = hs_f[n] * spread_mult + impact
                            sp_c = last_sc[n]; fp_c = last_fc[n]
                            sp_x = sp_c * (1 + s * hs_sx); fp_x = fp_c * (1 - s * hs_fx)
                            if f_exitstate > 0.5:      # whole-episode state: realised price pnl of partial exits + all fees paid + funding
                                est = ep_price[n] + pos[n] * s * (sp_avg[n] - sp_x) + pos[n] * s * (fp_x - fp_avg[n]) + fund_acc[n] - fees_ep[n] \
                                      - pos[n] * (sp_x * fee_s + fp_x * fee_f)
                            else:
                                est = pos[n] * s * (sp_avg[n] - sp_x) + pos[n] * s * (fp_x - fp_avg[n]) + fund_acc[n] - fees_acc[n] \
                                      - pos[n] * (sp_x * fee_s + fp_x * fee_f)
                            if est >= 0: do_exit = True
                            else:
                                if wait[n] == 0:
                                    wait[n] = 1; wait_h[n] = 0; wait_paid[n] = 0.0
                                wait_h[n] += 1
                                if wait_h[n] >= wmax: do_exit = True
                                elif wait_paid[n] >= wait_cap * ent_notional[n]: do_exit = True
                    else:
                        wait[n] = 0; wait_h[n] = 0; wait_paid[n] = 0.0
                if (not do_exit) and max_hold_h > 0 and (t - ep_start[n]) >= max_hold_h: do_exit = True
            # de-risk while holding: the position must stay within the t-1-based volume / OI limits
            want_trim = 0.0
            if (not do_exit) and derisk > 0.5 and have_bar:
                if f_hardcap > 1.5:                      # soft targets, OI measured on PERP notional (reviewer's P2-02a): trim the larger of the two excesses
                    ex_v = 0.0; ex_o = 0.0
                    if vol_frac > 0: ex_v = pos[n] * spot_mid - vol_frac * dv[t, n]
                    ex_o = (pos[n] * fut_mid - oilim[t, n]) * spot_mid / fut_mid
                    ex_ = max(ex_v, ex_o)
                    if ex_ > 0 and pos[n] * spot_mid > 0 and ex_ > derisk_tol * pos[n] * spot_mid + 1e-9: want_trim = ex_
                elif f_hardcap > 0.5:                      # hard caps in coin units: volume cap on spot notional, OI cap on PERP notional (both at t-1 closes)
                    xl = 1e300
                    if vol_frac > 0 and last_sc[n] > 0: xl = vol_frac * dv[t, n] / last_sc[n]
                    if last_fc[n] > 0:
                        xo = oilim[t, n] / last_fc[n]
                        if xo < xl: xl = xo
                    if pos[n] > xl * (1.0 + derisk_tol) + 1e-12: want_trim = (pos[n] - xl) * spot_mid
                else:
                    lim = 1e300
                    if vol_frac > 0: lim = vol_frac * dv[t, n]
                    if oilim[t, n] < lim: lim = oilim[t, n]
                    cur = pos[n] * spot_mid
                    if cur > lim * (1.0 + derisk_tol) + 1e-9: want_trim = cur - lim
            is_dr = False
            if f_mtmderisk > 0.5 and mtm_f > 0.0 and (not do_exit) and have_bar:     # capital cap at market value: trim every open position pro rata (shared participation budget applies below)
                tr_ = mtm_f * pos[n] * spot_mid
                if tr_ > want_trim: want_trim = tr_; is_dr = True
            if f_exitpov > 0.5:
                if t >= t1 - wind_down_h: do_exit = True      # window end: start unwinding wind_down_h before the end, at normal participation
            else:
                if last_hr: do_exit = True
            if (not do_exit) and want_trim <= 0.0: continue
            if do_exit and exiting[n] == 0:
                exiting[n] = 1; exit_start[n] = t
            hs_sx = hs_s[n] * spread_mult; hs_fx = hs_f[n] * spread_mult
            spot_trade = not (s < 0 and mirror_spot_trade < 0.5)
            Qpos = pos[n] * spot_mid
            Qwant = Qpos if do_exit else min(Qpos, want_trim)
            pv = pov if s > 0 else pov_m; xm = xmax if s > 0 else xmax_m; df = dfrac if s > 0 else dfrac_m
            if f_exitpov < 0.5 and do_exit and (last_hr or (t - exit_start[n]) > exit_force_h or not have_bar):
                Q = Qpos
            else:
                mm = budget(tb_, n, exec_mode, pv, xm, df, rho, spot_trade, sq, fq, d02, d1, r2, r5, xs, ds)
                if f_budget > 0.5: mm = mm - bud_used[n]
                Q = min(Qwant, mm if mm > 0 else 0.0)
                if derisk_force > 0.5 and (not do_exit) and want_trim > 0.0:    # v10: cut back to the cap in the same hour, outside the participation budget
                    Q = Qwant
            if Q <= 0: continue
            knots(d02[tb_, n], d1[tb_, n], r2[n], r5[n], xs, ds)
            if sqrt_y > 0:                       # square-root impact (sensitivity): per leg  sqrt_y * sigma_day * sqrt(Q / ADV), sigma/ADV known at t-1
                slf = sqrt_y * sg[t, n] * np.sqrt(Q / max(dv[t, n], 1.0)); sls = slf
            elif slip_mode > 0.5:
                slf = avg_slip(Q, 1.0, xs, ds); sls = avg_slip(Q, rho, xs, ds)
            else:
                slf = impact; sls = impact
            cs = (1 - mk_share) * (hs_sx + sls); cf = (1 - mk_share) * (hs_fx + slf)
            x = Q / spot_mid
            if x > pos[n]: x = pos[n]
            sp_x = spot_mid * (1 + s * cs); fp_x = fut_mid * (1 - s * cf)
            fs_ = (1 - mk_share) * fee_s + mk_share * fee_s_mk; ff_ = (1 - mk_share) * fee_f + mk_share * fee_f_mk
            frac = x / pos[n]
            price = s * x * (fp_x - fp_avg[n])
            fees = x * fp_x * ff_
            cost = x * fut_mid * cf
            if spot_trade:
                price += s * x * (sp_avg[n] - sp_x)
                fees += x * sp_x * fs_
                cost += x * spot_mid * cs
            tp[li] += price; tfe[li] += fees; tc[li] += cost; ep_price[n] += price
            if is_dr and not do_exit: diag[16] += x * spot_mid; diag[17] += fees + cost
            fees_ep[n] += fees; nfill_ep[n] += 1.0; bud_used[n] += x * spot_mid
            tu[li] += x * spot_mid; nfill[li] += 1
            fees_acc[n] -= fees_acc[n] * frac
            pos[n] -= x
            used_cap -= used_n[n] * frac * capf[n]
            if s < 0: used_pos -= used_n[n] * frac * capf[n]
            used_n[n] *= (1 - frac)
            if pos[n] <= 1e-12 or (pos[n] * spot_mid < 1e-6):
                pos[n] = 0.0
                used_cap -= used_n[n] * capf[n]
                if s < 0: used_pos -= used_n[n] * capf[n]
                used_n[n] = 0.0
                if n_ep < ep_buf.shape[0]:
                    ep_buf[n_ep, 0] = n; ep_buf[n_ep, 1] = ep_start[n]; ep_buf[n_ep, 2] = t
                    ep_buf[n_ep, 3] = ep_price[n] + fund_acc[n]; ep_buf[n_ep, 4] = ep_max_u[n]; ep_buf[n_ep, 5] = s
                    ep_buf[n_ep, 6] = fees_ep[n]; ep_buf[n_ep, 7] = nfill_ep[n]
                n_ep += 1; nep[li] += 1
                exiting[n] = 0; wait[n] = 0; wait_h[n] = 0; wait_paid[n] = 0.0; fund_acc[n] = 0.0; ep_price[n] = 0.0
                fees_ep[n] = 0.0; nfill_ep[n] = 0.0; n_adds[n] = 0.0
                side[n] = 0.0
        if f_mtmderisk > 0.5:                                       # diagnostics: usage left above S at the t-1 closes after pass 1 (trim limited by the participation budget)
            u1 = 0.0
            for n in range(N):
                if pos[n] > 0 and last_sc[n] > 0: u1 += pos[n] * last_sc[n] * capf[n]
            if u1 > capital * 1.0001:
                diag[20] += 1.0
                if u1 / capital > diag[21]: diag[21] = u1 / capital
        # ---------- pass 2: entries / adds
        if t < t1 - wind_down_h and t > 0:
            nc = 0
            for n in range(N):
                if not (sv[t, n] > 0 and fv[t, n] > 0): continue
                r = r8[t, n]
                if not np.isfinite(r): continue
                if exiting[n] > 0: continue
                if k_imp > 0.0 and pos[n] > 0 and r < 0.0:
                    o_ = oiv[t, n]; pf_ = fc[t - 1, n] if fc[t - 1, n] > 0 else last_fc[n]
                    if o_ > 0 and pf_ > 0: r = min(r + k_imp * 1e-4 * (pos[n] * pf_ / o_) * 100.0, 0.0)
                if not (last_sc[n] > 0 and last_fc[n] > 0): continue
                sc1 = sc[t - 1, n]; fc1 = fc[t - 1, n]
                if not (sc1 > 0 and fc1 > 0): continue
                b = (fc1 - sc1) / sc1
                s_new = 0.0; fav = 0.0
                if ok_n[n] > 0 and -r > entry_fav_n and b <= entry_basis_n:
                    s_new = 1.0; fav = -r
                elif mirror_on > 0.5 and ok_m[t, n] > 0:
                    rs = r8s[t, n]
                    if np.isfinite(rs) and rs > entry_fav_m and r > 0 and b >= entry_basis_m:
                        s_new = -1.0; fav = 1000.0 + rs   # neg leg has priority regardless of rate size
                if s_new == 0.0: continue
                if s_new > 0 and pos[n] <= 0 and n_streak > 1.5 and dv[t, n] >= big_thr and streak[t, n] < n_streak: continue   # v10: big coins need N consecutive entry-grade prints
                if s_new > 0: fav = 2000.0 + fav        # neg leg first
                if pos[n] > 0:
                    if side[n] != s_new: continue
                    if s_new > 0 and add_n < 0.5: continue
                    if s_new < 0 and add_m < 0.5: continue
                    if f_addonce > 0.5 and n_adds[n] >= max_adds: continue     # one top-up per episode
                cand_n[nc] = n; cand_f[nc] = fav; cand_s[nc] = s_new; nc += 1
            if nc > 0:
                ucm = 0.0
                if f_mtmcap > 1.5 or f_mtmderisk > 0.5:  # (sensitivity) capital room measured at market value (t-1 closes), single-leg notional
                    for n in range(N):
                        if pos[n] > 0 and last_sc[n] > 0: ucm += pos[n] * last_sc[n] * capf[n]
                order = np.argsort(-cand_f[:nc])
                for oi in range(nc):
                    c = order[oi]; n = cand_n[c]; s = cand_s[c]; li = 0 if s > 0 else 1
                    spot_trade = not (s < 0 and mirror_spot_trade < 0.5)
                    spot_mid = sv[t, n]; fut_mid = fv[t, n]
                    if s > 0: cm = 1.0
                    else: cm = (1.0 + margin_m) if spot_trade else margin_m
                    ccap = cap_n[n] if s > 0 else cap_m[n]
                    if f_coin > 0.5:                     # inventory cap in coin units; USDT caps are converted with the t-1 close each hour
                        coins_cap = cap_q[n] if (cap_q[n] > 0 and s > 0) else ccap / last_sc[n]
                        room_coin = max(0.0, coins_cap - pos[n]) * spot_mid
                    else:
                        room_coin = ccap - used_n[n]
                    ref_u = ucm if f_mtmcap > 1.5 else used_cap
                    if f_mtmderisk > 0.5: ref_u = max(ucm, used_cap)      # both the market-value and the cost-basis usage must stay <= capital
                    room_cap = (capital - ref_u) / cm
                    if s < 0:
                        r2_ = (pos_cap_frac * capital - used_pos) / cm
                        if r2_ < room_cap: room_cap = r2_
                    pv = pov if s > 0 else pov_m; xm = xmax if s > 0 else xmax_m; df = dfrac if s > 0 else dfrac_m
                    Q0 = budget(tb_, n, exec_mode, pv, xm, df, rho, spot_trade, sq, fq, d02, d1, r2, r5, xs, ds)
                    if exec_mode < 1.5 and f_budget > 0.5: Q0 = Q0 - bud_used[n]
                    if exec_mode < 1.5 and not (Q0 > 0): Q0 = 0.0
                    Q = min(Q0, min(room_coin, room_cap))
                    if f_hardcap > 1.5:                  # soft targets (nothing forces a trim), OI on PERP notional
                        if vol_frac > 0:
                            room_v = vol_frac * dv[t, n] - pos[n] * spot_mid
                            if room_v < Q: Q = room_v
                        room_oi = (oilim[t, n] - pos[n] * fut_mid) * spot_mid / fut_mid
                        if room_oi < Q: Q = room_oi
                    elif f_hardcap > 0.5:                  # hard caps in coin units at t-1 closes: volume on spot notional, OI on PERP notional
                        xl = 1e300
                        if vol_frac > 0: xl = vol_frac * dv[t, n] / last_sc[n]
                        xo = oilim[t, n] / last_fc[n]
                        if xo < xl: xl = xo
                        room_h = (xl - pos[n]) * spot_mid
                        if room_h < Q: Q = room_h
                    else:
                        cur_n = pos[n] * spot_mid                 # current notional (0 when flat)
                        if vol_frac > 0:                          # dv = trailing-24h volume up to t-1 (causal); dv == 0 -> no room
                            room_v = vol_frac * dv[t, n] - cur_n
                            if room_v < Q: Q = room_v
                        room_oi = oilim[t, n] - cur_n             # lagged OI limit (0 when OI unknown)
                        if room_oi < Q: Q = room_oi
                    if s > 0 and room_cap < min(Q0, room_coin) and room_cap < Q0: blocked += 1
                    if not (Q > min_u and Q > 0): continue
                    knots(d02[tb_, n], d1[tb_, n], r2[n], r5[n], xs, ds)
                    if sqrt_y > 0:
                        slf = sqrt_y * sg[t, n] * np.sqrt(Q / max(dv[t, n], 1.0)); sls = slf
                    elif slip_mode > 0.5:
                        slf = avg_slip(Q, 1.0, xs, ds); sls = avg_slip(Q, rho, xs, ds)
                    else:
                        slf = impact; sls = impact
                    cs = (1 - mk_share) * (hs_s[n] * spread_mult + sls); cf = (1 - mk_share) * (hs_f[n] * spread_mult + slf)
                    x = Q / spot_mid
                    sp_e = spot_mid * (1 - s * cs); fp_e = fut_mid * (1 + s * cf)
                    fs_ = (1 - mk_share) * fee_s + mk_share * fee_s_mk; ff_ = (1 - mk_share) * fee_f + mk_share * fee_f_mk
                    fees = x * fp_e * ff_; cost = x * fut_mid * cf
                    if spot_trade:
                        fees += x * sp_e * fs_; cost += x * spot_mid * cs
                    if pos[n] == 0:
                        side[n] = s; sp_avg[n] = sp_e; fp_avg[n] = fp_e; ep_start[n] = t; ent_notional[n] = Q
                        fund_acc[n] = 0.0; ep_price[n] = 0.0; ep_max_u[n] = 0.0; wait[n] = 0; wait_h[n] = 0; wait_paid[n] = 0.0
                        capf[n] = cm; fees_ep[n] = 0.0; nfill_ep[n] = 0.0; n_adds[n] = 0.0
                    else:
                        tw = pos[n] + x
                        sp_avg[n] = (sp_avg[n] * pos[n] + sp_e * x) / tw
                        fp_avg[n] = (fp_avg[n] * pos[n] + fp_e * x) / tw
                        ent_notional[n] += Q
                        n_adds[n] += 1.0
                    pos[n] += x
                    fees_acc[n] += fees; tfe[li] += fees; tc[li] += cost
                    fees_ep[n] += fees; nfill_ep[n] += 1.0; bud_used[n] += Q
                    used_n[n] += Q; used_cap += Q * cm; ucm += Q * cm
                    if s < 0: used_pos += Q * cm
                    if used_n[n] > ep_max_u[n]: ep_max_u[n] = used_n[n]
                    tu[li] += Q; nfill[li] += 1
        # ---------- bookkeeping: MTM cum net (valued at this hour's close - after all decisions of the hour)
        mtm = 0.0; um = 0.0; ucost = used_cap
        for n in range(N):
            cst = sc[t, n] if sc[t, n] > 0 else last_sc[n]
            cft = fc[t, n] if fc[t, n] > 0 else last_fc[n]
            if pos[n] > 0:
                s = side[n]; li = 0 if s > 0 else 1
                um += pos[n] * cst * capf[n]
                if f_mtmcap > 0.5:
                    area[li] += pos[n] * cst * capf[n]
                else:
                    area[li] += used_n[n] * capf[n]
                if cst > 0 and cft > 0:
                    mtm += s * pos[n] * (sp_avg[n] - cst) + s * pos[n] * (cft - fp_avg[n])
                diag[11] += 1.0
                if t > 0 and sc[t - 1, n] > 0 and fc[t - 1, n] > 0:   # hard-cap audit at the hour end, valued at the t-1 closes the limits are set with (same in every mode)
                    if vol_frac > 0:
                        lv = vol_frac * dv[t, n]
                        rv = pos[n] * sc[t - 1, n] / lv if lv > 0 else 1e9
                        if rv > 1.0 + 1e-9: diag[0] += 1.0
                        if lv > 0 and rv > diag[2]: diag[2] = rv
                    lo = oilim[t, n]
                    ro = pos[n] * fc[t - 1, n] / lo if lo > 0 else 1e9
                    if lo < 1e299:
                        if ro > 1.0 + 1e-9: diag[1] += 1.0
                        if lo > 0 and ro > diag[3]: diag[3] = ro
            if bud_used[n] > 0:                                   # participation audit: fills of the hour vs prior-hour / same-hour volume
                pv_ = pov
                a1 = sq[t - 1, n] if t > 0 else 0.0; b1 = fq[t - 1, n] if t > 0 else 0.0
                a1 = a1 if a1 > 0 else 0.0; b1 = b1 if b1 > 0 else 0.0
                m1 = min(a1, b1)
                if m1 > 0:
                    q1 = bud_used[n] / m1
                    if q1 > diag[5]: diag[5] = q1
                    if q1 > pv_ * (1.0 + 1e-9): diag[4] += 1.0
                else:
                    diag[4] += 1.0
                a0 = sq[t, n]; b0 = fq[t, n]
                a0 = a0 if a0 > 0 else 0.0; b0 = b0 if b0 > 0 else 0.0
                m0 = min(a0, b0)
                if m0 > 0:
                    q0_ = bud_used[n] / m0
                    if q0_ > diag[7]: diag[7] = q0_
                    if q0_ > pv_ * (1.0 + 1e-9): diag[6] += 1.0
                else:
                    diag[6] += 1.0
        for n in range(N):                                        # t-1 closes for the next hour's decisions
            if sc[t, n] > 0: last_sc[n] = sc[t, n]
            if fc[t, n] > 0: last_fc[n] = fc[t, n]
        if f_mtmderisk > 0.5:
            if um > capital * 1.0001: diag[18] += 1.0
            if mtm_f > 0.0: diag[19] += 1.0
        if ucost > diag[12]: diag[12] = ucost
        if um > diag[13]: diag[13] = um
        diag[14] += um; diag[15] += ucost
        out_used[t - t0] = um if f_mtmcap > 0.5 else used_cap
        out_comp[t - t0, 0] = tf[0] + tf[1]; out_comp[t - t0, 1] = tp[0] + tp[1]; out_comp[t - t0, 2] = tfe[0] + tfe[1]
        out_comp[t - t0, 3] = tc[0] + tc[1]; out_comp[t - t0, 4] = tu[0] + tu[1]; out_comp[t - t0, 5] = mtm
        out_cum[t - t0] = tp[0] + tp[1] + tf[0] + tf[1] - tfe[0] - tfe[1] + mtm
    if f_exitpov > 0.5:
        # window end: positions that could not be unwound within participation are NOT closed at an unlimited size; their liquidation
        # (20bp impact + half-spread + fees at the last close) is charged in the accounting so the reported net is a closed-out number, and the
        # residual notional is reported separately in diag[8].
        for n in range(N):
            if pos[n] > 0 and last_sc[n] > 0 and last_fc[n] > 0:
                s = side[n]; li = 0 if s > 0 else 1; x = pos[n]
                cs = (1 - mk_share) * (hs_s[n] * spread_mult + impact); cf = (1 - mk_share) * (hs_f[n] * spread_mult + impact)
                sp_x = last_sc[n] * (1 + s * cs); fp_x = last_fc[n] * (1 - s * cf)
                fs_ = (1 - mk_share) * fee_s + mk_share * fee_s_mk; ff_ = (1 - mk_share) * fee_f + mk_share * fee_f_mk
                price = s * x * (fp_x - fp_avg[n]) + s * x * (sp_avg[n] - sp_x)
                fees = x * fp_x * ff_ + x * sp_x * fs_
                cost = x * last_fc[n] * cf + x * last_sc[n] * cs
                tp[li] += price; tfe[li] += fees; tc[li] += cost; tu[li] += x * last_sc[n]; nfill[li] += 1
                ep_price[n] += price; fees_ep[n] += fees; nfill_ep[n] += 1.0
                diag[8] += x * last_sc[n]; diag[9] += fees + cost
                if n_ep < ep_buf.shape[0]:
                    ep_buf[n_ep, 0] = n; ep_buf[n_ep, 1] = ep_start[n]; ep_buf[n_ep, 2] = t1
                    ep_buf[n_ep, 3] = ep_price[n] + fund_acc[n]; ep_buf[n_ep, 4] = ep_max_u[n]; ep_buf[n_ep, 5] = s
                    ep_buf[n_ep, 6] = fees_ep[n]; ep_buf[n_ep, 7] = nfill_ep[n]
                n_ep += 1; nep[li] += 1
                pos[n] = 0.0
        lt = t1 - 1 - t0
        out_comp[lt, 1] = tp[0] + tp[1]; out_comp[lt, 2] = tfe[0] + tfe[1]; out_comp[lt, 3] = tc[0] + tc[1]; out_comp[lt, 4] = tu[0] + tu[1]
        out_comp[lt, 5] = 0.0
        out_cum[lt] = tp[0] + tp[1] + tf[0] + tf[1] - tfe[0] - tfe[1]
    res = np.zeros((2, 8))
    for li in range(2):
        res[li, 0] = tp[li]; res[li, 1] = tf[li]; res[li, 2] = tfe[li]; res[li, 3] = tc[li]; res[li, 4] = tu[li]
        res[li, 5] = nfill[li]; res[li, 6] = nep[li]; res[li, 7] = area[li] / max(t1 - t0, 1)
    return res, blocked, lost_f
