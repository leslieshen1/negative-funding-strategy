#!/usr/bin/env python3
"""
[v11_full: copy of borrow_lag/engine_bl.py + (1) delisting forced exit / delivery liquidation, (2) maker execution modes (mkm=1: perp maker + spot taker hedge; mkm=2: both maker),
 (3) binding-constraint counters (xd), (4) tick-size penetration. All maker fills are estimated from hourly klines only.]
[borrow_lag copy of ../code/engine.py: ONLY change = ok_n is a (T,N) time-varying tradability mask, used at the entry/add gate]
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
      "derisk_force", "n_streak", "big_thr",
      # v11 maker modes: mkm 0 = taker both legs (v10), 1 = perp maker + spot taker hedge, 2 = both legs maker (reference);
      # pen = relative penetration through the limit (0.002 = 0.2%), pen_k > 0 -> penetration in ticks instead; fshare = share of eligible fills that is counted
      "mkm", "pen", "pen_k", "fshare", "advw",
      # v12: cutb_sqrt = coefficient y (0 = off): with derisk_force=1 the part of a same-hour cut-back above the pov budget is charged sqrt-law impact; advmode 1 = adverse selection charged once (see mk_fill)
      "cutb_sqrt", "advmode",
      # v12s exit-signal family: xmode 0 = off (old exit_fav_n/wait logic); 1 = X1 funding >= 0 (old path with exit_fav_n=1e-9, wait 0); 2 = X2 funding>=0 for 24h continuous; 3 = X3 72h without an hour below -0.05% (8h-eq);
      # 4 = X4 168h without an hour below -0.05%; 5 = X5 cumulative funding of the last 24h (settled during the hold) turned net paying (>0). All use the own-impact-adjusted rate of the position.
      "xmode", "x_bad",
      # v12s2: x_obs 1 = exit TRIGGER uses the observed settled market rate (no own-impact term; own impact still hits PnL); xmode 6 = X6 observed 8h-eq rate > x6_thr; xmode 7 = X7 x7_h * max(0, -sum24/24) < x7_cost
      # (sum24 = observed settled funding of the last 24h, passed in as s24); gap_sym 1 = next-hour taker gap of the maker modes is priced signed (gains too)
      "x_obs", "x6_thr", "x7_cost", "x7_h", "gap_sym",
      # v12s3: cool_h = same-coin re-entry cooldown after a full close (hours; 0 = off); hx_off 1 = maker hedge leg priced at the normal taker price instead of the hour extreme; gap_sym 2 = next-hour gap cost switched off
      "cool_h", "hx_off",
      # v12s4: conf_h = the exit condition (xmode>=2) must hold on conf_h consecutive hourly checks before the exit fires (1 = off)
      "conf_h", "sch_mode", "sch_w",
      # v12s7: use_xa 1 = per coin-hour arrays xa[0]=impact-coefficient multiplier (x sqrt_y), xa[1]=half-spread multiplier, xa[2]=EV-exit ratio (expected remaining income / close cost);
      # x_ev_k>0: EV exit fires when xa[2] < x_ev_k for x_ev_h consecutive hours | x_tp_act/x_tp_gb>0: trailing take-profit on cumulative episode funding (fraction of entry notional): armed at act, exit on give-back gb
      # x_br_fr>0: basis-reversion exit when the basis has closed >= x_br_fr of its entry gap.  All of them are OR-ed with the xmode exit.
      "use_xa", "x_ev_k", "x_ev_h", "x_tp_act", "x_tp_gb", "x_br_fr",
      # round 9 (external audit 226d73d), ALL default 0 = round-8 behaviour: fx_fund 1 = own-impact funding NOT clipped at zero for settled negative events (a positive result is a payment), 2 = additionally applied to settled positive events;
      # fx_eq 1 = entry/add capital budget = current equity (capital + cum PnL at the previous close) - usage, 2 = min(capital, equity) (losses reduce the budget, profits do not raise it);
      # res_mode (window-end residual): 0 legacy flat cost, 1 = pov-respecting unwind COST (slices of the prior-hour participation budget, normal impact law per slice), 2 = conservative MTM close (legacy cost x res_mult);
      # leg_dly 1 = spot leg fills at the NEXT hour open, 2 = perp leg fills at the next hour open (entries and exits; leg-risk sensitivity)
      "fx_fund", "fx_eq", "res_mode", "res_mult", "leg_dly",
      # round 9b: imp_size 1 = impact-aware sizing: entries/adds capped so that the marginal own-impact funding bump (2*k_imp*1e-2*q/OI per 8h) does not exceed the expected funding |r8| (q <= 50*|r8|*OI/k_imp); existing positions are not trimmed
      "imp_size",
      # round 10 (two-stage exit, default off): x2_frac>0 = when the exit CONDITION has held x2_conf1 consecutive hourly checks (1 = immediately) close the fraction x2_frac of the position at once (pov-limited, worked off over the following hours),
      # once per episode; the remainder is closed by the normal exit (conf_h consecutive checks). A pending target is cancelled if the position is added to.
      "x2_frac", "x2_conf1"]
PI = {k: i for i, k in enumerate(PN)}
DEFAULT = dict(entry_fav_n=0.001, entry_basis_n=1.0, exit_fav_n=0.0, wait_max_n=24, wait_cap=0.001,
               mirror_on=0, entry_fav_m=0.00012, entry_basis_m=-1.0, exit_fav_m=0.00005, wait_max_m=0, mirror_spot_trade=1, margin_m=1.0,
               pov=0.05, xmax=0.002, dfrac=0.5, rho=1.0, slip_mode=0, impact=0.0002,
               fee_s=0.001, fee_f=0.0005, fee_s_mk=0.001, fee_f_mk=0.0002, mk_share=0.0, spread_mult=1.0,
               add_n=0, add_m=1, max_hold_h=0, exit_force_h=336, wind_down_h=24, capital=3e6, min_u=0.0, pos_cap_frac=1.0,
               pov_m=0.05, xmax_m=0.01, dfrac_m=0.5, exec_mode=0, vol_frac=0.0, derisk=1, derisk_tol=0.0,
               f_exitlag=0, f_markfund=0, f_coin=0, f_budget=0, f_prior=0, f_hardcap=0, f_exitpov=0, f_addonce=0, f_exitstate=0, f_mtmcap=0,
               max_adds=1, k_imp=0.0, sqrt_y=0.0, f_mtmderisk=0,
               derisk_force=0, n_streak=1, big_thr=0.0, mkm=0.0, pen=0.002, pen_k=0.0, fshare=1.0, advw=1.0, cutb_sqrt=0.0, advmode=0.0, xmode=0.0, x_bad=0.0005, x_obs=0.0, x6_thr=0.0001, x7_cost=0.0, x7_h=168.0, gap_sym=0.0, cool_h=0.0, hx_off=0.0, conf_h=1.0, sch_mode=0.0, sch_w=12.0, use_xa=0.0, x_ev_k=0.0, x_ev_h=6.0, x_tp_act=0.0, x_tp_gb=0.0, x_br_fr=0.0, fx_fund=0.0, fx_eq=0.0, res_mode=0.0, res_mult=3.0, leg_dly=0.0, imp_size=0.0, x2_frac=0.0, x2_conf1=1.0)

def mkp(**kw):
    d = dict(DEFAULT); d.update(kw)
    bad = set(kw) - set(PN)
    assert not bad, bad
    return np.array([d[k] for k in PN], dtype=np.float64)

@njit(cache=False)
def knots(d02, d1, r2, r5, xs, ds):
    if not (d1 > 0): d1 = 0.0
    if not (d02 > 0): d02 = 0.0
    if d02 > 0.95 * d1: d02 = 0.95 * d1
    xs[0] = 0.0; ds[0] = 0.0
    xs[1] = 0.002; ds[1] = d02
    xs[2] = 0.01; ds[2] = d1
    xs[3] = 0.02; ds[3] = d1 * max(r2, 1.05)
    xs[4] = 0.05; ds[4] = d1 * max(r5, r2 * 1.05, 1.1)

@njit(cache=False)
def D_at(x, xs, ds):
    if x <= 0: return 0.0
    for k in range(4):
        if x <= xs[k + 1]:
            return ds[k] + (ds[k + 1] - ds[k]) * (x - xs[k]) / (xs[k + 1] - xs[k])
    return ds[4]

@njit(cache=False)
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

@njit(cache=False)
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

@njit(cache=False)
def mk_fill(entry, t, n, mode, pen, pen_k, fshare, so, sh, sl, fo, fh, fl, sc, fc, tk_s, tk_f, spot_mid, fut_mid, cs_tk, cf_tk,
            fee_s, fee_f, fee_s_mk, fee_f_mk, TT, advw, advmode, hxoff):
    """Maker leg(s) of ONE order on the hourly bar. Limit = HOUR OPEN of the current hour (= the previous close, known at order time). Perp is the maker leg (mode 1), spot too in mode 2.
    entry (side +1): perp BUY limit at fo, spot SELL limit at so.  exit: perp SELL limit at fo, spot BUY limit at so.
    A leg is eligible only if the hourly extreme trades THROUGH the limit by pen (relative) or pen_k ticks (touch alone does not count); counted fill = fshare x quantity.
    The other leg of the filled quantity is hedged in the same hour and pays the taker fee. hxoff=1 (v16 default via run_headline): hedge at the normal taker model price (mid + calibrated spread/impact).
    hxoff=0 (stress only): hedge at the ADVERSE EXTREME of the hour (high for a buy, low for a sell; never better than the normal taker price).
    Adverse selection (advw scales it): advmode 0 = adverse move of the maker-leg price from the limit to the NEXT hour close (>=0); 1 = charged once, both-maker part only;
    2 = PAIR basis change, both-maker part only (so it is 0 when only one leg is maker, i.e. mkm 1 or 3); 3 (v16 default) = advw x hourly half-range per maker fill.
    returns (qpair, cs_eff, cf_eff, fs_blend, ff_blend, adv_unit, elig_f, elig_s, hedge_extra_unit)"""
    Lf = fo[t, n]; Ls = so[t, n]
    ef = False; es = False
    if Lf > 0 and fl[t, n] > 0 and fh[t, n] > 0:
        thr = pen_k * tk_f[n] if (pen_k > 0 and tk_f[n] > 0) else pen * Lf
        if entry: ef = fl[t, n] <= Lf - thr
        else: ef = fh[t, n] >= Lf + thr
    if mode > 2.5: ef = False            # v16: mode 3 = spot maker + perp taker hedge
    if mode > 1.5 and Ls > 0 and sl[t, n] > 0 and sh[t, n] > 0:
        thr = pen_k * tk_s[n] if (pen_k > 0 and tk_s[n] > 0) else pen * Ls
        if entry: es = sh[t, n] >= Ls + thr
        else: es = sl[t, n] <= Ls - thr
    fill_f = fshare if ef else 0.0
    fill_s = fshare if es else 0.0
    qb = min(fill_f, fill_s); qp = max(fill_f, fill_s)
    if not (qp > 0): return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ef, es, 0.0
    fpm = fill_f; fph = fill_s - qb; fsm = fill_s; fsh = fill_f - qb
    if entry:
        Hp = fut_mid * (1 + cf_tk); Hs = spot_mid * (1 - cs_tk)
        if hxoff < 0.5 and fh[t, n] > Hp: Hp = fh[t, n]                      # buy hedge at the hour HIGH when that is worse than the taker price
        if hxoff < 0.5 and sl[t, n] > 0 and sl[t, n] < Hs: Hs = sl[t, n]      # sell hedge at the hour LOW
        Pf = (fpm * Lf + fph * Hp) / qp; Ps = (fsm * Ls + fsh * Hs) / qp
        advf = 0.0; advs = 0.0
        if t + 1 < TT:
            if fc[t + 1, n] > 0: advf = max(0.0, Lf - fc[t + 1, n]) * fpm / qp
            if sc[t + 1, n] > 0 and fsm > 0: advs = max(0.0, sc[t + 1, n] - Ls) * fsm / qp
        hx = (fph * (Hp - fut_mid * (1 + cf_tk)) + fsh * (spot_mid * (1 - cs_tk) - Hs)) / qp
        if advmode > 0.5:                                   # v12: charge ONCE. A leg whose other leg is hedged at the taker/extreme price in the same hour is already charged by the hedge price; adverse selection only for the both-maker part (single-leg exposure), larger leg only
            advf = 0.0; advs = 0.0
            if qb > 0 and t + 1 < TT and fc[t + 1, n] > 0 and sc[t + 1, n] > 0:
                af_ = max(0.0, Lf - fc[t + 1, n]); as_ = max(0.0, sc[t + 1, n] - Ls)
                if af_ / Lf >= as_ / Ls: advf = af_ * qb / qp
                else: advs = as_ * qb / qp
            if advmode > 1.5:                                # v12s5 advmode=2: PAIR adverse selection = unfavourable move of the limit BASIS (perp - spot) to the next close (a hedged pair does not lose on the outright price move)
                advf = 0.0; advs = 0.0
                if qb > 0 and t + 1 < TT and fc[t + 1, n] > 0 and sc[t + 1, n] > 0:
                    advf = max(0.0, (Lf - Ls) - (fc[t + 1, n] - sc[t + 1, n])) * qb / qp
        if advmode > 2.5:                                # v16 advmode=3: adverse selection = advw x hourly half-range per MAKER fill, against us
            advf = advw * 0.5 * (fh[t, n] - fl[t, n]) * fpm / qp if (fh[t, n] > 0 and fl[t, n] > 0) else 0.0
            advs = advw * 0.5 * (sh[t, n] - sl[t, n]) * fsm / qp if (sh[t, n] > 0 and sl[t, n] > 0) else 0.0
        else:
            advf *= advw; advs *= advw
        Pf_e = Pf + advf; Ps_e = Ps - advs
        cf = Pf_e / fut_mid - 1.0; cs = 1.0 - Ps_e / spot_mid
        fee_f_amt = (fpm * Lf * fee_f_mk + fph * Hp * fee_f) / qp
        fee_s_amt = (fsm * Ls * fee_s_mk + fsh * Hs * fee_s) / qp
        return qp, cs, cf, fee_s_amt / Ps_e, fee_f_amt / Pf_e, advf + advs, ef, es, hx
    else:
        Hp = fut_mid * (1 - cf_tk); Hs = spot_mid * (1 + cs_tk)
        if hxoff < 0.5 and fl[t, n] > 0 and fl[t, n] < Hp: Hp = fl[t, n]      # sell hedge (perp) at the hour LOW
        if hxoff < 0.5 and sh[t, n] > Hs: Hs = sh[t, n]                       # buy hedge (spot) at the hour HIGH
        Pf = (fpm * Lf + fph * Hp) / qp; Ps = (fsm * Ls + fsh * Hs) / qp
        advf = 0.0; advs = 0.0
        if t + 1 < TT:
            if fc[t + 1, n] > 0: advf = max(0.0, fc[t + 1, n] - Lf) * fpm / qp
            if sc[t + 1, n] > 0 and fsm > 0: advs = max(0.0, Ls - sc[t + 1, n]) * fsm / qp
        hx = (fph * (fut_mid * (1 - cf_tk) - Hp) + fsh * (Hs - spot_mid * (1 + cs_tk))) / qp
        if advmode > 0.5:
            advf = 0.0; advs = 0.0
            if qb > 0 and t + 1 < TT and fc[t + 1, n] > 0 and sc[t + 1, n] > 0:
                af_ = max(0.0, fc[t + 1, n] - Lf); as_ = max(0.0, Ls - sc[t + 1, n])
                if af_ / Lf >= as_ / Ls: advf = af_ * qb / qp
                else: advs = as_ * qb / qp
            if advmode > 1.5:
                advf = 0.0; advs = 0.0
                if qb > 0 and t + 1 < TT and fc[t + 1, n] > 0 and sc[t + 1, n] > 0:
                    advf = max(0.0, (fc[t + 1, n] - sc[t + 1, n]) - (Lf - Ls)) * qb / qp
        if advmode > 2.5:                                # v16 advmode=3: adverse selection = advw x hourly half-range per MAKER fill, against us
            advf = advw * 0.5 * (fh[t, n] - fl[t, n]) * fpm / qp if (fh[t, n] > 0 and fl[t, n] > 0) else 0.0
            advs = advw * 0.5 * (sh[t, n] - sl[t, n]) * fsm / qp if (sh[t, n] > 0 and sl[t, n] > 0) else 0.0
        else:
            advf *= advw; advs *= advw
        Pf_e = Pf - advf; Ps_e = Ps + advs
        cf = 1.0 - Pf_e / fut_mid; cs = Ps_e / spot_mid - 1.0
        fee_f_amt = (fpm * Lf * fee_f_mk + fph * Hp * fee_f) / qp
        fee_s_amt = (fsm * Ls * fee_s_mk + fsh * Hs * fee_s) / qp
        return qp, cs, cf, fee_s_amt / Ps_e, fee_f_amt / Pf_e, advf + advs, ef, es, hx

@njit(cache=False)
def wgt(prof, h, n, mode):
    """child-slice weight of hour h: TWAP = 1; VWAP = past-only same-hour-of-day volume profile (built from hours t-24d, d>=1, never the current hour)"""
    if mode < 1.5: return 1.0
    hh = min(h, prof.shape[0] - 1)
    v = prof[hh, n]
    return v if v > 1e-9 else 1e-9

@njit(cache=False)
def sim(t0, t1, sv, sc, sq, fv, fc, fq, ev, r8, r8s, d02, d1, dv, oilim, hs_s, hs_f, r2, r5, ok_n, ok_m, cap_n, cap_m,
        mk, cap_q, oiv, ivh, sg, streak, P, out_used, out_cum, ep_buf, out_comp, diag, pos_rec,
        so, sh, sl, fo, fh, fl, tk_s, tk_f, dl_exit, dl_fin, xd, s24, prof, xa, fsm):
    """xd (float64[64]) v11 counters: 0-4 candidate hours whose BINDING room is [inventory, OI, volume, pov, capital] | 5-9 notional unlocked if that binding constraint were removed (up to the next constraint) | 10-14 filled notional in those hours | 15 candidate hours | 16 filled notional (all)
    20-23 held coin-hours at >=98% of [inventory, OI, volume] / none | 25-28 notional-weighted | 29 total held notional-hours | 30 maker attempts | 31 maker attempts with an eligible leg | 32 pair notional filled with a maker leg | 33 notional deferred to next-hour taker
    34 next-hour-taker fills | 35 leg-gap cost (USDT) | 36 adverse-selection cost | 37 hedge-at-extreme extra cost vs normal taker price | 38 maker-filled notional (entry) | 39 maker-filled notional (exit)
    40 delisting forced exits (episodes) | 41 residual notional liquidated at delivery | 42 cost of that liquidation | 43 hours entries were blocked by the delisting rule"""
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
    fees_ep = np.zeros(N); cost_ep = np.zeros(N); lf_ep = np.zeros(N); n_adds = np.zeros(N); nfill_ep = np.zeros(N); bud_used = np.zeros(N)
    last_sc = np.zeros(N); last_fc = np.zeros(N)
    for n in range(N):                       # last known closes before the first simulated hour (t0-1 and earlier)
        for tb in range(t0 - 1, -1, -1):
            if last_sc[n] <= 0 and sc[tb, n] > 0: last_sc[n] = sc[tb, n]
            if last_fc[n] <= 0 and fc[tb, n] > 0: last_fc[n] = fc[tb, n]
            if last_sc[n] > 0 and last_fc[n] > 0: break
    xs = np.zeros(5); ds = np.zeros(5)
    ev_ct = np.zeros(N); tp_peak = np.zeros(N); b_ent = np.zeros(N)
    conf_ct = np.zeros(N); last_close = np.full(N, -1e9); since_bad = np.zeros(N); nonneg = np.zeros(N); ring = np.zeros((N, 24))
    used_cap = 0.0; used_pos = 0.0
    tp = np.zeros(2); tf = np.zeros(2); tfe = np.zeros(2); tc = np.zeros(2); tu = np.zeros(2)
    nfill = np.zeros(2); nep = np.zeros(2); area = np.zeros(2)
    blocked = 0.0
    n_ep = 0
    cand_n = np.zeros(N, dtype=np.int64); cand_f = np.zeros(N); cand_s = np.zeros(N)
    mkm = P[56]; pen = P[57]; pen_k = P[58]; fshare = P[59]; advw = P[60]; cutb_sqrt = P[61]; advmode = P[62]; xmode = P[63]; x_bad = P[64]; x_obs = P[65]; x6_thr = P[66]; x7_cost = P[67]; x7_h = P[68]; gap_sym = P[69]; cool_h = P[70]; hx_off = P[71]; conf_h = P[72]; sch_mode = P[73]; sch_w = int(P[74]); use_xa = P[75]; x_ev_k = P[76]; x_ev_h = P[77]; x_tp_act = P[78]; x_tp_gb = P[79]; x_br_fr = P[80]; exc_n = 0.0; cutb_n = 0.0
    fx_fund = P[81]; fx_eq = P[82]; res_mode = P[83]; res_mult = P[84]; leg_dly = P[85]; imp_size = P[86]; x2_frac = P[87]; x2_conf1 = P[88]
    x2_done = np.zeros(N); x2_tgt = np.zeros(N)
    sch_on = np.zeros(N); sch_t0 = np.zeros(N); sch_rem = np.zeros(N); sch_last = np.full(N, -5.0)
    tk_now = np.zeros(N); tk_set = np.zeros(N); gap_ref = np.zeros(N); gap_kind = np.zeros(N); bv = np.zeros(5)
    dl_cnt = np.zeros(N)
    for t in range(t0, t1):
        last_hr = (t == t1 - 1)
        tb_ = t - 1 if (f_prior > 0.5 and t > 0) else t          # hour index of the volume / depth known when the decision is taken
        for n in range(N):
            bud_used[n] = 0.0
            tk_now[n] = tk_set[n]; tk_set[n] = 0.0
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
        # ---------- v11: delivery of a delisted contract: whatever is still open is liquidated at the last close with taker costs (impact + half spread + fees)
        for n in range(N):
            if pos[n] > 0 and t >= dl_fin[n] and last_sc[n] > 0 and last_fc[n] > 0:
                s = side[n]; li = 0 if s > 0 else 1; x = pos[n]
                cs = (1 - mk_share) * (hs_s[n] * spread_mult + impact); cf = (1 - mk_share) * (hs_f[n] * spread_mult + impact)
                sp_x = last_sc[n] * (1 + s * cs); fp_x = last_fc[n] * (1 - s * cf)
                price = s * x * (fp_x - fp_avg[n]) + s * x * (sp_avg[n] - sp_x)
                fees = x * fp_x * fee_f + x * sp_x * (fee_s*fsm[n])
                cost = x * last_fc[n] * cf + x * last_sc[n] * cs
                tp[li] += price; tfe[li] += fees; tc[li] += cost; cost_ep[n] += cost; tu[li] += x * last_sc[n]; nfill[li] += 1
                ep_price[n] += price; fees_ep[n] += fees; nfill_ep[n] += 1.0
                xd[41] += x * last_sc[n]; xd[42] += fees + cost; xd[48] += price + cost
                used_cap -= used_n[n] * capf[n]
                used_n[n] = 0.0
                if n_ep < ep_buf.shape[0]:
                    ep_buf[n_ep, 0] = n; ep_buf[n_ep, 1] = ep_start[n]; ep_buf[n_ep, 2] = t
                    ep_buf[n_ep, 3] = ep_price[n] + fund_acc[n]; ep_buf[n_ep, 4] = ep_max_u[n]; ep_buf[n_ep, 5] = s
                    ep_buf[n_ep, 6] = fees_ep[n]; ep_buf[n_ep, 7] = nfill_ep[n]; ep_buf[n_ep, 8] = fund_acc[n]; ep_buf[n_ep, 9] = cost_ep[n]; ep_buf[n_ep, 10] = lf_ep[n]
                n_ep += 1; nep[li] += 1
                pos[n] = 0.0; last_close[n] = t; exiting[n] = 0; wait[n] = 0; wait_h[n] = 0; wait_paid[n] = 0.0; fund_acc[n] = 0.0; ep_price[n] = 0.0
                fees_ep[n] = 0.0; cost_ep[n] = 0.0; lf_ep[n] = 0.0; nfill_ep[n] = 0.0; n_adds[n] = 0.0; side[n] = 0.0
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
                if d8 > 0.0 and (e < 0.0 or fx_fund > 1.5):
                    iv_ = ivh[t, n] if ivh[t, n] > 0 else 8.0
                    e2 = e + d8 * iv_ / 8.0
                    if fx_fund < 0.5: e2 = min(e2, 0.0)             # legacy (round 8): the own-impact term could only cancel income, never create a payment
                    lost_f += (e2 - e) * mk_ * pos[n]; lf_ep[n] += (e2 - e) * mk_ * pos[n]; e = e2
                fnd = -s * e * mk_ * pos[n]
                fund_acc[n] += fnd; tf[li] += fnd
                if wait[n] > 0 and fnd < 0: wait_paid[n] += -fnd
            ring[n, t % 24] = e
            have_bar = (sv[t, n] > 0) and (fv[t, n] > 0)
            if (not have_bar) and ((not last_hr) or f_exitpov > 0.5): continue        # missing bar: defer (fixed) / stale-price close in the last hour (legacy)
            spot_mid = sv[t, n] if sv[t, n] > 0 else last_sc[n]
            fut_mid = fv[t, n] if fv[t, n] > 0 else last_fc[n]
            do_exit = False
            if exiting[n] > 0:
                do_exit = True
            else:
                r = r8[t, n] if s > 0 else r8s[t, n]
                r_raw = r
                if d8 > 0.0 and np.isfinite(r) and r < 0.0: r = min(r + d8, 0.0)
                if x_obs > 0.5: r = r_raw
                if np.isfinite(r):
                    fav = -s * r
                    if xmode > 1.5:
                        if -r > x_bad: since_bad[n] = 0.0
                        else: since_bad[n] += 1.0
                        if r >= 0.0: nonneg[n] += 1.0
                        else: nonneg[n] = 0.0
                        xe = False
                        if xmode < 2.5: xe = nonneg[n] >= 24.0
                        elif xmode < 3.5: xe = since_bad[n] >= 72.0
                        elif xmode < 4.5: xe = since_bad[n] >= 168.0
                        elif xmode > 7.5: xe = r >= 0.0                                   # r11: X8 single settlement >= 0 (latest settled rate), confirmation via conf_h
                        elif xmode > 6.5: xe = x7_h * max(0.0, -s24[t, n] / 24.0) < x7_cost
                        elif xmode > 5.5: xe = r > x6_thr
                        elif x_obs > 0.5: xe = s24[t, n] > 0.0
                        else:
                            sr = 0.0
                            for k_ in range(24): sr += ring[n, k_]
                            xe = sr > 0.0
                        xe_raw = xe
                        if conf_h > 1.5 or x2_frac > 0.0:
                            if xe: conf_ct[n] += 1.0
                            else: conf_ct[n] = 0.0
                            if conf_h > 900.0: xe = conf_ct[n] >= 2.0 and ev[t, n] != 0.0     # r11: wait for the coin's NEXT settlement after the trigger (still satisfied)
                            elif conf_h > 1.5: xe = conf_ct[n] >= conf_h
                        if x2_frac > 0.0 and xe_raw and x2_done[n] < 0.5 and conf_ct[n] >= x2_conf1 and pos[n] > 0:
                            x2_done[n] = 1.0; x2_tgt[n] = pos[n] * (1.0 - x2_frac)
                        if xe: do_exit = True
                    if (not do_exit) and pos[n] > 0:                 # v12s7 candidate exits (OR-ed with the xmode exit)
                        if x_ev_k > 0.0 and use_xa > 0.5:
                            if np.isfinite(xa[2, t, n]) and xa[2, t, n] < x_ev_k: ev_ct[n] += 1.0
                            else: ev_ct[n] = 0.0
                            if ev_ct[n] >= x_ev_h: do_exit = True
                        if x_tp_act > 0.0 and ent_notional[n] > 0:
                            cf_ = fund_acc[n] / ent_notional[n]
                            if cf_ > tp_peak[n]: tp_peak[n] = cf_
                            if tp_peak[n] >= x_tp_act and cf_ <= tp_peak[n] - x_tp_gb: do_exit = True
                        if x_br_fr > 0.0 and b_ent[n] < 0.0 and last_sc[n] > 0 and last_fc[n] > 0:
                            if ((last_fc[n] - last_sc[n]) / last_sc[n] - b_ent[n]) >= x_br_fr * (-b_ent[n]): do_exit = True
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
                                      - pos[n] * (sp_x * (fee_s*fsm[n]) + fp_x * fee_f)
                            else:
                                est = pos[n] * s * (sp_avg[n] - sp_x) + pos[n] * s * (fp_x - fp_avg[n]) + fund_acc[n] - fees_acc[n] \
                                      - pos[n] * (sp_x * (fee_s*fsm[n]) + fp_x * fee_f)
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
            is_x2 = False
            if f_mtmderisk > 0.5 and mtm_f > 0.0 and (not do_exit) and have_bar:     # capital cap at market value: trim every open position pro rata (shared participation budget applies below)
                tr_ = mtm_f * pos[n] * spot_mid
                if tr_ > want_trim: want_trim = tr_; is_dr = True
            if x2_tgt[n] > 0.0 and (not do_exit) and have_bar and pos[n] > x2_tgt[n] * (1.0 + 1e-9):      # round 10: stage-1 of the two-stage exit (work the position down to the target, pov-limited)
                tr_ = (pos[n] - x2_tgt[n]) * spot_mid
                if tr_ > want_trim: want_trim = tr_; is_x2 = True
            forced = False
            if f_exitpov > 0.5:
                if t >= t1 - wind_down_h: do_exit = True; forced = True      # window end: start unwinding wind_down_h before the end, at normal participation
            else:
                if last_hr: do_exit = True; forced = True
            if t >= dl_exit[n]:                                # v11: delisting announcement proxy (delivery - 3 days): force exit, taker
                do_exit = True; forced = True
                if dl_cnt[n] == 0: dl_cnt[n] = 1.0; xd[40] += 1.0
            if (not do_exit) and want_trim <= 0.0: continue
            if do_exit and exiting[n] == 0:
                exiting[n] = 1; exit_start[n] = t
                if not forced:
                    xd[46] += 1.0
                    if np.isfinite(r8[t, n]) and r8[t, n] < 0.0: xd[47] += 1.0
            hsm_ = xa[1, t, n] if use_xa > 0.5 else 1.0
            hs_sx = hs_s[n] * spread_mult * hsm_; hs_fx = hs_f[n] * spread_mult * hsm_
            spot_trade = not (s < 0 and mirror_spot_trade < 0.5)
            Qpos = pos[n] * spot_mid
            Qwant = Qpos if do_exit else min(Qpos, want_trim)
            if sch_mode > 0.5 and do_exit and (t < dl_exit[n]) and (not last_hr) and (t - exit_start[n]) < sch_w - 1:
                # v12s5: signal exit locked at trigger time; the remaining position is worked off over sch_w hours (TWAP / past-only VWAP weights)
                kk_ = int(t - exit_start[n]); ws_ = 0.0
                for j_ in range(kk_, sch_w): ws_ += wgt(prof, int(exit_start[n]) + j_, n, sch_mode)
                Qwant = min(Qwant, Qpos * wgt(prof, t, n, sch_mode) / ws_)
            pv = pov if s > 0 else pov_m; xm = xmax if s > 0 else xmax_m; df = dfrac if s > 0 else dfrac_m
            exc_q = 0.0
            if f_exitpov < 0.5 and do_exit and (last_hr or (t - exit_start[n]) > exit_force_h or not have_bar):
                Q = Qpos
            else:
                mm = budget(tb_, n, exec_mode, pv, xm, df, rho, spot_trade, sq, fq, d02, d1, r2, r5, xs, ds)
                if f_budget > 0.5: mm = mm - bud_used[n]
                Q = min(Qwant, mm if mm > 0 else 0.0)
                if derisk_force > 1.5 and (not do_exit) and want_trim > 0.0 and (not is_x2):   # v12s fix: forced de-risk OBEYS the pov / participation budget (worked off over hours); the part not yet cut is residual over-cap exposure (diagnostics xd[52..55])
                    xd[52] += 1.0; xd[56] += Qwant
                    lim_n = Qpos - want_trim
                    if lim_n <= 1e-9: xd[57] += 1.0                       # zero cap this hour (volume or OI snapshot missing / zero): cap is unknown, not a real limit
                    else:
                        ratio_ = Qpos / lim_n
                        if ratio_ > xd[54]: xd[54] = ratio_
                        if Qwant > Q + 1e-9:
                            xd[53] += Qwant - Q; xd[55] += 1.0
                            if ratio_ < 1.25: xd[58] += 1.0
                            elif ratio_ < 2.0: xd[59] += 1.0
                            elif ratio_ < 5.0: xd[60] += 1.0
                            else: xd[61] += 1.0
                if 0.5 < derisk_force < 1.5 and (not do_exit) and want_trim > 0.0:    # v10: cut back to the cap in the same hour, outside the participation budget
                    Q = Qwant
                    if Q > mm and mm > 0: exc_q = Q - mm
                    elif Q > 0 and mm <= 0: exc_q = Q
                    if Q > 0: cutb_n += Q; exc_n += exc_q
            if Q <= 0: continue
            knots(d02[tb_, n], d1[tb_, n], r2[n], r5[n], xs, ds)
            if sqrt_y > 0:                       # square-root impact (sensitivity): per leg  sqrt_y * sigma_day * sqrt(Q / ADV), sigma/ADV known at t-1
                slf = sqrt_y * (xa[0, t, n] if use_xa > 0.5 else 1.0) * sg[t, n] * np.sqrt(Q / max(dv[t, n], 1.0)); sls = slf
            elif slip_mode > 0.5:
                slf = avg_slip(Q, 1.0, xs, ds); sls = avg_slip(Q, rho, xs, ds)
            else:
                slf = impact; sls = impact
            if cutb_sqrt > 0 and exc_q > 0:                  # v12: excess over the pov budget in a same-hour cut-back: max(fixed, sqrt-law) on the excess part
                ex_ = cutb_sqrt * (xa[0, t, n] if use_xa > 0.5 else 1.0) * sg[t, n] * np.sqrt(exc_q / max(dv[t, n], 1.0))
                wq_ = exc_q / Q
                slf = (1 - wq_) * slf + wq_ * max(slf, ex_); sls = (1 - wq_) * sls + wq_ * max(sls, ex_)
            cs = (1 - mk_share) * (hs_sx + sls); cf = (1 - mk_share) * (hs_fx + slf)
            use_o = False; fs_o = 0.0; ff_o = 0.0
            if mkm > 0.5 and s > 0 and do_exit and (not forced) and tk_now[n] < 0.5 and spot_trade:
                xd[30] += 1.0
                qp, cs_m, cf_m, fs_o, ff_o, adv_, ef_, es_, hx_ = mk_fill(False, t, n, mkm, pen, pen_k, fshare, so, sh, sl, fo, fh, fl, sc, fc, tk_s, tk_f, spot_mid, fut_mid, cs, cf,
                                                                          (fee_s*fsm[n]), fee_f, (fee_s_mk*fsm[n]), fee_f_mk, TT, advw, advmode, hx_off)
                if ef_ or es_: xd[31] += 1.0
                if qp <= 0.0:
                    tk_set[n] = 1.0; gap_ref[n] = fo[t, n]; gap_kind[n] = -1.0; xd[33] += Q
                    continue
                if qp < 1.0 - 1e-9:
                    tk_set[n] = 1.0; gap_ref[n] = fo[t, n]; gap_kind[n] = -1.0; xd[33] += Q * (1.0 - qp)
                xd[32] += Q * qp; xd[39] += Q * qp
                Q = Q * qp
                cs = cs_m; cf = cf_m; use_o = True
                xd[36] += adv_ * Q / spot_mid; xd[37] += hx_ * Q / spot_mid
            elif do_exit and tk_now[n] > 0.5 and gap_kind[n] < 0 and gap_ref[n] > 0:
                gp_ = 0.0 if gap_sym > 1.5 else ((gap_ref[n] - fut_mid) if gap_sym > 0.5 else max(0.0, gap_ref[n] - fut_mid))             # leg-gap: price moved against the planned maker limit by the time of the next-hour taker
                cf += gp_ / fut_mid; xd[35] += gp_ * Q / spot_mid; xd[34] += Q
            x = Q / spot_mid
            if x > pos[n]: x = pos[n]
            spx_ = spot_mid; fpx_ = fut_mid
            if t + 1 < TT:
                if 0.5 < leg_dly < 1.5 and sv[t + 1, n] > 0: spx_ = sv[t + 1, n]
                elif leg_dly > 1.5 and fv[t + 1, n] > 0: fpx_ = fv[t + 1, n]
            sp_x = spx_ * (1 + s * cs); fp_x = fpx_ * (1 - s * cf)
            fs_ = (1 - mk_share) * (fee_s*fsm[n]) + mk_share * (fee_s_mk*fsm[n]); ff_ = (1 - mk_share) * fee_f + mk_share * fee_f_mk
            if use_o: fs_ = fs_o; ff_ = ff_o
            frac = x / pos[n]
            price = s * x * (fp_x - fp_avg[n])
            fees = x * fp_x * ff_
            cost = x * fut_mid * cf
            if spot_trade:
                price += s * x * (sp_avg[n] - sp_x)
                fees += x * sp_x * fs_
                cost += x * spot_mid * cs
            tp[li] += price; tfe[li] += fees; tc[li] += cost; cost_ep[n] += cost; ep_price[n] += price
            if is_dr and not do_exit: diag[16] += x * spot_mid; diag[17] += fees + cost
            fees_ep[n] += fees; nfill_ep[n] += 1.0; bud_used[n] += x * spot_mid
            tu[li] += x * spot_mid; nfill[li] += 1
            fees_acc[n] -= fees_acc[n] * frac
            pos[n] -= x
            used_cap -= used_n[n] * frac * capf[n]
            if s < 0: used_pos -= used_n[n] * frac * capf[n]
            used_n[n] *= (1 - frac)
            if pos[n] <= 1e-12 or (pos[n] * spot_mid < 1e-6):
                pos[n] = 0.0; last_close[n] = t
                used_cap -= used_n[n] * capf[n]
                if s < 0: used_pos -= used_n[n] * capf[n]
                used_n[n] = 0.0
                if n_ep < ep_buf.shape[0]:
                    ep_buf[n_ep, 0] = n; ep_buf[n_ep, 1] = ep_start[n]; ep_buf[n_ep, 2] = t
                    ep_buf[n_ep, 3] = ep_price[n] + fund_acc[n]; ep_buf[n_ep, 4] = ep_max_u[n]; ep_buf[n_ep, 5] = s
                    ep_buf[n_ep, 6] = fees_ep[n]; ep_buf[n_ep, 7] = nfill_ep[n]; ep_buf[n_ep, 8] = fund_acc[n]; ep_buf[n_ep, 9] = cost_ep[n]; ep_buf[n_ep, 10] = lf_ep[n]
                n_ep += 1; nep[li] += 1
                exiting[n] = 0; wait[n] = 0; wait_h[n] = 0; wait_paid[n] = 0.0; fund_acc[n] = 0.0; ep_price[n] = 0.0
                fees_ep[n] = 0.0; cost_ep[n] = 0.0; lf_ep[n] = 0.0; nfill_ep[n] = 0.0; n_adds[n] = 0.0
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
                if t >= dl_exit[n]: continue                  # v11: no new entry / add after the delisting announcement proxy
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
                if ok_n[t, n] > 0 and -r > entry_fav_n and b <= entry_basis_n and (cool_h <= 0.0 or pos[n] > 0 or t - last_close[n] >= cool_h):
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
                        if f_coin > 1.5: room_coin = min(room_coin, max(0.0, ccap - used_n[n]))      # round 9: coin cap AND the USDT cap (never above either)
                    else:
                        room_coin = ccap - used_n[n]
                    ref_u = ucm if f_mtmcap > 1.5 else used_cap
                    if f_mtmderisk > 0.5: ref_u = max(ucm, used_cap)      # both the market-value and the cost-basis usage must stay <= capital
                    cap_eff = capital
                    if fx_eq > 0.5 and t > t0:
                        cap_eff = capital + out_cum[t - 1 - t0]
                        if fx_eq > 1.5 and cap_eff > capital: cap_eff = capital
                        if cap_eff < 0.0: cap_eff = 0.0
                    room_cap = (cap_eff - ref_u) / cm
                    if s < 0:
                        r2_ = (pos_cap_frac * capital - used_pos) / cm
                        if r2_ < room_cap: room_cap = r2_
                    pv = pov if s > 0 else pov_m; xm = xmax if s > 0 else xmax_m; df = dfrac if s > 0 else dfrac_m
                    Q0 = budget(tb_, n, exec_mode, pv, xm, df, rho, spot_trade, sq, fq, d02, d1, r2, r5, xs, ds)
                    if exec_mode < 1.5 and f_budget > 0.5: Q0 = Q0 - bud_used[n]
                    if exec_mode < 1.5 and not (Q0 > 0): Q0 = 0.0
                    Q0r = Q0
                    if sch_mode > 0.5: Q0 = 1e300
                    Q = min(Q0, min(room_coin, room_cap))
                    if imp_size > 0.5 and k_imp > 0.0 and s > 0 and oiv[t, n] > 0.0:
                        e_exp_ = -r8[t, n] if (np.isfinite(r8[t, n]) and r8[t, n] < 0.0) else 0.0
                        ri_ = 50.0 * e_exp_ * oiv[t, n] / k_imp - pos[n] * spot_mid
                        if ri_ < Q: Q = ri_
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
                    # v11 binding-constraint counter (candidate = signal on, tradable, bars present): which room is the smallest
                    rv_ = 1e300; ro_ = 1e300
                    if vol_frac > 0: rv_ = (vol_frac * dv[t, n] / last_sc[n] - pos[n]) * spot_mid
                    ro_ = (oilim[t, n] / last_fc[n] - pos[n]) * spot_mid
                    bv[0] = max(room_coin, 0.0); bv[1] = max(ro_, 0.0); bv[2] = max(rv_, 0.0); bv[3] = max(Q0, 0.0); bv[4] = max(room_cap, 0.0)
                    kmin = 0
                    for kk in range(1, 5):
                        if bv[kk] < bv[kmin]: kmin = kk
                    m2 = 1e300
                    for kk in range(5):
                        if kk != kmin and bv[kk] < m2: m2 = bv[kk]
                    xd[kmin] += 1.0; xd[15] += 1.0
                    if m2 < 1e200: xd[5 + kmin] += (m2 - bv[kmin])
                    if sch_mode > 0.5 and s > 0:
                        # v12s5 entry schedule: target = room (inventory/OI/volume/capital caps) at the start hour; child slice = remaining target x w_t / sum of remaining weights,
                        # capped by the same-hour pov budget; the entry signal is RE-CHECKED at every child hour (a missed hour cancels the schedule)
                        Qr_ = max(Q, 0.0)
                        if not (sch_on[n] > 0.5 and sch_last[n] == t - 1 and (t - sch_t0[n]) < sch_w):
                            sch_on[n] = 1.0; sch_t0[n] = t; sch_rem[n] = Qr_
                        sch_last[n] = t
                        kk_ = int(t - sch_t0[n])
                        if kk_ >= sch_w - 1: sl_ = sch_rem[n]
                        else:
                            ws_ = 0.0
                            for j_ in range(kk_, sch_w): ws_ += wgt(prof, int(sch_t0[n]) + j_, n, sch_mode)
                            sl_ = sch_rem[n] * wgt(prof, t, n, sch_mode) / ws_
                        Q = min(min(sl_, Q0r), Qr_)
                    if not (Q > min_u and Q > 0): continue
                    knots(d02[tb_, n], d1[tb_, n], r2[n], r5[n], xs, ds)
                    if sqrt_y > 0:
                        slf = sqrt_y * (xa[0, t, n] if use_xa > 0.5 else 1.0) * sg[t, n] * np.sqrt(Q / max(dv[t, n], 1.0)); sls = slf
                    elif slip_mode > 0.5:
                        slf = avg_slip(Q, 1.0, xs, ds); sls = avg_slip(Q, rho, xs, ds)
                    else:
                        slf = impact; sls = impact
                    hsm_ = xa[1, t, n] if use_xa > 0.5 else 1.0
                    cs = (1 - mk_share) * (hs_s[n] * spread_mult * hsm_ + sls); cf = (1 - mk_share) * (hs_f[n] * spread_mult * hsm_ + slf)
                    use_o = False; fs_o = 0.0; ff_o = 0.0
                    if mkm > 0.5 and s > 0 and tk_now[n] < 0.5 and spot_trade:
                        xd[30] += 1.0
                        qp, cs_m, cf_m, fs_o, ff_o, adv_, ef_, es_, hx_ = mk_fill(True, t, n, mkm, pen, pen_k, fshare, so, sh, sl, fo, fh, fl, sc, fc, tk_s, tk_f, spot_mid, fut_mid, cs, cf,
                                                                                  (fee_s*fsm[n]), fee_f, (fee_s_mk*fsm[n]), fee_f_mk, TT, advw, advmode, hx_off)
                        if ef_ or es_: xd[31] += 1.0
                        if qp <= 0.0:
                            tk_set[n] = 1.0; gap_ref[n] = fo[t, n]; gap_kind[n] = 1.0; xd[33] += Q
                            continue
                        if qp < 1.0 - 1e-9:
                            tk_set[n] = 1.0; gap_ref[n] = fo[t, n]; gap_kind[n] = 1.0; xd[33] += Q * (1.0 - qp)
                        xd[32] += Q * qp; xd[38] += Q * qp
                        Q = Q * qp
                        cs = cs_m; cf = cf_m; use_o = True
                        xd[36] += adv_ * Q / spot_mid; xd[37] += hx_ * Q / spot_mid
                    elif tk_now[n] > 0.5 and gap_kind[n] > 0 and gap_ref[n] > 0 and s > 0:
                        gp_ = 0.0 if gap_sym > 1.5 else ((fut_mid - gap_ref[n]) if gap_sym > 0.5 else max(0.0, fut_mid - gap_ref[n]))
                        cf += gp_ / fut_mid; xd[35] += gp_ * Q / spot_mid; xd[34] += Q
                    x = Q / spot_mid
                    spx_ = spot_mid; fpx_ = fut_mid
                    if t + 1 < TT:
                        if 0.5 < leg_dly < 1.5 and sv[t + 1, n] > 0: spx_ = sv[t + 1, n]
                        elif leg_dly > 1.5 and fv[t + 1, n] > 0: fpx_ = fv[t + 1, n]
                    sp_e = spx_ * (1 - s * cs); fp_e = fpx_ * (1 + s * cf)
                    fs_ = (1 - mk_share) * (fee_s*fsm[n]) + mk_share * (fee_s_mk*fsm[n]); ff_ = (1 - mk_share) * fee_f + mk_share * fee_f_mk
                    if use_o: fs_ = fs_o; ff_ = ff_o
                    fees = x * fp_e * ff_; cost = x * fut_mid * cf
                    if spot_trade:
                        fees += x * sp_e * fs_; cost += x * spot_mid * cs
                    if pos[n] == 0:
                        side[n] = s; sp_avg[n] = sp_e; fp_avg[n] = fp_e; ep_start[n] = t; ent_notional[n] = Q
                        fund_acc[n] = 0.0; ep_price[n] = 0.0; ep_max_u[n] = 0.0; wait[n] = 0; wait_h[n] = 0; wait_paid[n] = 0.0
                        since_bad[n] = 0.0; nonneg[n] = 0.0; conf_ct[n] = 0.0; x2_done[n] = 0.0; x2_tgt[n] = 0.0; ev_ct[n] = 0.0; tp_peak[n] = 0.0; b_ent[n] = (fut_mid - spot_mid) / spot_mid
                        for k_ in range(24): ring[n, k_] = 0.0
                        capf[n] = cm; fees_ep[n] = 0.0; cost_ep[n] = 0.0; lf_ep[n] = 0.0; nfill_ep[n] = 0.0; n_adds[n] = 0.0
                    else:
                        tw = pos[n] + x
                        sp_avg[n] = (sp_avg[n] * pos[n] + sp_e * x) / tw
                        fp_avg[n] = (fp_avg[n] * pos[n] + fp_e * x) / tw
                        ent_notional[n] += Q
                        n_adds[n] += 1.0
                        x2_tgt[n] = 0.0
                    pos[n] += x
                    if sch_mode > 0.5: sch_rem[n] = max(sch_rem[n] - Q, 0.0)
                    fees_acc[n] += fees; tfe[li] += fees; tc[li] += cost; cost_ep[n] += cost
                    fees_ep[n] += fees; nfill_ep[n] += 1.0; bud_used[n] += Q
                    used_n[n] += Q; used_cap += Q * cm; ucm += Q * cm
                    if s < 0: used_pos += Q * cm
                    if used_n[n] > ep_max_u[n]: ep_max_u[n] = used_n[n]
                    tu[li] += Q; nfill[li] += 1
                    xd[10 + kmin] += Q; xd[16] += Q
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
                if t > 0 and sc[t - 1, n] > 0 and fc[t - 1, n] > 0:
                    u_i = used_n[n] / cap_n[n] if cap_n[n] > 0 else 0.0
                    lv_ = vol_frac * dv[t, n]; lo_ = oilim[t, n]
                    u_v = pos[n] * sc[t - 1, n] / lv_ if lv_ > 0 else 1e9
                    u_o = pos[n] * fc[t - 1, n] / lo_ if lo_ > 0 else 1e9
                    kb = 3; ub = 0.98
                    if u_i >= ub: kb = 0; ub = u_i
                    if u_o >= ub: kb = 1; ub = u_o
                    if u_v >= ub: kb = 2; ub = u_v
                    wq = pos[n] * cst
                    xd[20 + kb] += 1.0; xd[25 + kb] += wq; xd[29] += wq
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
            pos_rec[t - t0, n] = pos[n] * last_sc[n]      # borrow_lag: single-leg position notional at the hour close
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
                diag[24] += 1.0
                if res_mode > 0.5:
                    qn_ = x * last_sc[n]; tr_ = t1 - 1
                    bq_ = budget(tr_, n, exec_mode, pov, xmax, dfrac, rho, True, sq, fq, d02, d1, r2, r5, xs, ds)
                    if res_mode > 1.5 or not (bq_ > 0.0):
                        cs = cs * res_mult; cf = cf * res_mult                  # conservative: legacy cost x res_mult (also when there is no participation budget at all)
                        if not (bq_ > 0.0): diag[27] += 1.0
                    else:
                        Qs_ = min(qn_, bq_); knots(d02[tr_, n], d1[tr_, n], r2[n], r5[n], xs, ds)
                        if sqrt_y > 0:
                            slf_ = sqrt_y * (xa[0, tr_, n] if use_xa > 0.5 else 1.0) * sg[tr_, n] * np.sqrt(Qs_ / max(dv[tr_, n], 1.0)); sls_ = slf_
                        elif slip_mode > 0.5:
                            slf_ = avg_slip(Qs_, 1.0, xs, ds); sls_ = avg_slip(Qs_, rho, xs, ds)
                        else:
                            slf_ = impact; sls_ = impact
                        cs = (1 - mk_share) * (hs_s[n] * spread_mult + sls_); cf = (1 - mk_share) * (hs_f[n] * spread_mult + slf_)
                        nh_ = np.ceil(qn_ / bq_)
                        if nh_ > diag[25]: diag[25] = nh_
                        diag[26] += qn_ * nh_
                sp_x = last_sc[n] * (1 + s * cs); fp_x = last_fc[n] * (1 - s * cf)
                fs_ = (1 - mk_share) * (fee_s*fsm[n]) + mk_share * (fee_s_mk*fsm[n]); ff_ = (1 - mk_share) * fee_f + mk_share * fee_f_mk
                price = s * x * (fp_x - fp_avg[n]) + s * x * (sp_avg[n] - sp_x)
                fees = x * fp_x * ff_ + x * sp_x * fs_
                cost = x * last_fc[n] * cf + x * last_sc[n] * cs
                tp[li] += price; tfe[li] += fees; tc[li] += cost; cost_ep[n] += cost; tu[li] += x * last_sc[n]; nfill[li] += 1
                ep_price[n] += price; fees_ep[n] += fees; nfill_ep[n] += 1.0
                diag[8] += x * last_sc[n]; diag[9] += fees + cost; xd[49] += price + cost
                if n_ep < ep_buf.shape[0]:
                    ep_buf[n_ep, 0] = n; ep_buf[n_ep, 1] = ep_start[n]; ep_buf[n_ep, 2] = t1
                    ep_buf[n_ep, 3] = ep_price[n] + fund_acc[n]; ep_buf[n_ep, 4] = ep_max_u[n]; ep_buf[n_ep, 5] = s
                    ep_buf[n_ep, 6] = fees_ep[n]; ep_buf[n_ep, 7] = nfill_ep[n]; ep_buf[n_ep, 8] = fund_acc[n]; ep_buf[n_ep, 9] = cost_ep[n]; ep_buf[n_ep, 10] = lf_ep[n]
                n_ep += 1; nep[li] += 1
                pos[n] = 0.0
        lt = t1 - 1 - t0
        out_comp[lt, 1] = tp[0] + tp[1]; out_comp[lt, 2] = tfe[0] + tfe[1]; out_comp[lt, 3] = tc[0] + tc[1]; out_comp[lt, 4] = tu[0] + tu[1]
        out_comp[lt, 5] = 0.0
        out_cum[lt] = tp[0] + tp[1] + tf[0] + tf[1] - tfe[0] - tfe[1]
    xd[44] = cutb_n; xd[45] = exc_n
    res = np.zeros((2, 8))
    for li in range(2):
        res[li, 0] = tp[li]; res[li, 1] = tf[li]; res[li, 2] = tfe[li]; res[li, 3] = tc[li]; res[li, 4] = tu[li]
        res[li, 5] = nfill[li]; res[li, 6] = nep[li]; res[li, 7] = area[li] / max(t1 - t0, 1)
    return res, blocked, lost_f
