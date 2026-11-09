"""Round 7 library: public-data impact calibration (past-only), per coin-hour cost, expected-income gate (funding + basis convergence), EV-exit ratio.
Everything is rolling / past-only; only aggregates are ever written out."""
import sys; sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from run_adapt import *
import lib12s as L
CAL = __import__("os").environ["NFB_DATA"].rstrip("/") + "/calib/"
SIGD = D.sigma(); DVD = D.dv
LAGD = 24                                                  # a sample day is usable 24h after its last hour (past-only)
sym2i = {s: i for i, s in enumerate(D.fut)}
def day_avail(d): return HABS(d) + 24 + LAGD               # hour index from which the sample of day d may be used

def stepwise(rows, K=3, shrink=0.5, lo=None, hi=None):
    """rows: list of (coin_idx, avail_hour, value). Returns (T,N) matrix: coin's mean of its last K samples, shrunk (weight `shrink`) toward the cross-sectional median of all samples
    available at that hour; coins without own samples get the pooled median. Piecewise-constant in time, past-only by construction (avail_hour includes the lag)."""
    rows = sorted(rows, key=lambda r: r[1]); av = sorted({r[1] for r in rows})
    M = np.full((T, N), np.nan); own = {n: [] for n in range(N)}; allv = []
    prev = 0
    for k, a in enumerate(av):
        for (n, h, v) in [r for r in rows if r[1] == a]:
            own[n].append(v); allv.append(v)
        pool = np.median(allv); nxt = av[k + 1] if k + 1 < len(av) else T
        col = np.full(N, pool)
        for n in range(N):
            if own[n]:
                m = np.mean(own[n][-K:]); col[n] = (1 - shrink) * m + shrink * pool
        M[a:nxt] = col[None, :]
    first = av[0]; M[:first] = M[first]                        # before the first usable sample: the first pooled estimate (that is pooled median of day-0 samples; used only for hours before window)
    if lo is not None: M = np.clip(M, lo, hi)
    return M

def load_cal():
    ag = pd.read_csv(CAL + "ag.csv"); bd = pd.read_csv(CAL + "bd.csv")
    ag = ag[ag.sym.isin(sym2i)]; bd = bd[bd.sym.isin(sym2i)]
    return ag, bd

def walk_slip(q, Nl):
    """average price move (fraction) to take notional q from a book whose cumulative notional within +-L% is Nl[L] (piecewise-linear in L from 0 at mid; NaN levels skipped)."""
    lv = [(0.0, 0.0)] + [(l / 100.0, Nl[l]) for l in (0.2, 1, 2, 3, 4, 5) if np.isfinite(Nl.get(l, np.nan))]
    Ls = np.array([x[0] for x in lv]); Ns = np.maximum.accumulate(np.array([x[1] for x in lv]))
    if len(Ls) < 3 or Ns[-1] <= 0: return np.nan
    qq = np.linspace(0, min(q, Ns[-1]), 400); xx = np.interp(qq, Ns, Ls)
    if q > Ns[-1]: qq = np.concatenate([qq, [q]]); xx = np.concatenate([xx, [0.10]])      # beyond 5% of book: charge 10% on the remainder
    return float(np.trapz(xx, qq) / q) if hasattr(np, "trapz") else float(np.trapezoid(xx, qq) / q)

def book_Y(bd):
    rows = []
    for r in bd.to_dict("records"):
        n = sym2i[r["sym"]]; idx = HABS(r["date"]) + 24
        if idx >= T: continue
        sg_, dv_ = SIGD[idx, n], DVD[idx, n]
        if not (np.isfinite(sg_) and np.isfinite(dv_) and dv_ > 0 and sg_ > 0): continue
        qref = 0.03 * dv_ / 24.0
        sl = [walk_slip(qref, {l: r.get(f"{side}{l:g}", np.nan) for l in (0.2, 1, 2, 3, 4, 5)}) for side in ("bid", "ask")]
        if not all(np.isfinite(sl)): continue
        rows.append((n, day_avail(r["date"]), 0.5 * sum(sl) / (sg_ * np.sqrt(qref / dv_))))
    return rows

def build_cal(mode="kyle"):
    """returns (ymat, hsm, info). mode 'kyle' = aggTrades sqrt-law coefficient; 'book' = bookDepth walk-the-book coefficient."""
    ag, bd = load_cal()
    ry = [(sym2i[r.sym], day_avail(r.date), r.y_kyle) for r in ag.itertuples() if np.isfinite(r.y_kyle) and r.y_kyle > 0]
    rh = []
    for r in ag.itertuples():
        if not np.isfinite(r.hs) or r.hs <= 0: continue
        n = sym2i[r.sym]; base = 0.5 * (D.hs_s[n] + D.hs_f[n]) if False else D.hs_f[n]
        if base > 0: rh.append((n, day_avail(r.date), r.hs / base))
    Yk = stepwise(ry, lo=0.1, hi=3.0)
    Hm = stepwise(rh, lo=0.25, hi=4.0)
    rb = book_Y(bd); Yb = stepwise(rb, lo=0.01, hi=3.0)
    info = dict(n_ag=len(ag), n_bd=len(bd), n_y=len(ry), n_hs=len(rh), n_yb=len(rb))
    return (Yk if mode == "kyle" else Yb), Hm, info, (ry, rh, rb)

# ---------------- slice cost per coin-hour (past-only)
CAPV = L.cap_vec(D, S, "all")
def slice_Q(): return np.minimum(0.03 * DVD / 24.0, np.where(CAPV > 0, CAPV, 1e18)[None, :])
def leg_cost(Y):                       # one leg, one hourly slice
    return Y * SIGD * np.sqrt(slice_Q() / np.maximum(DVD, 1.0))
def entry_cost(Y, Hm):                 # round trip: fees 0.30% + 4 legs impact + 2 x (hs_s + hs_f) x spread multiplier
    return 0.0030 + 4 * leg_cost(Y) + 2 * (D.hs_s + D.hs_f)[None, :] * Hm
def close_cost(Y, Hm):                 # exit only: fees 0.15% + 2 legs impact + (hs_s+hs_f)
    return 0.0015 + 2 * leg_cost(Y) + (D.hs_s + D.hs_f)[None, :] * Hm

# ---------------- expected income tables (past-only, monthly refresh, 90d lookback, outcomes complete before the month start)
BB_E = np.array([-0.012, -0.006]); NBB = 3                       # basis buckets for entries: <=-1.2%, -1.2..-0.6, -0.6..-0.3
BBKT = np.clip(np.digitize(b_prev, BB_E), 0, 2)
F24, END24, FIN24 = sample_outcomes(24)
bnow = (fc - sc) / np.where(sc > 0, sc, np.nan)
_end = np.minimum(END24, T - 1)
DB24 = np.take_along_axis(bnow, _end, axis=0) - b_prev          # basis change over the sample (pair price pnl per unit notional)
def ei_tables(NM=200):
    ef = np.full((len(MSTART), NB), np.nan); eb = np.full((len(MSTART), NBB), np.nan); e2 = np.full((len(MSTART), NB, NBB), np.nan)
    for i, M in enumerate(MSTART):
        lo = max(0, M - LOOK); sel = SIG[lo:M] & FIN24[lo:M] & (END24[lo:M] < M) & np.isfinite(DB24[lo:M])
        f = F24[lo:M]; d = DB24[lo:M]; rb = BKT[lo:M]; bb = BBKT[lo:M]
        for b in range(NB):
            m = sel & (rb == b)
            if m.sum() >= NM: ef[i, b] = f[m].mean()
        for k in range(NBB):
            m = sel & (bb == k)
            if m.sum() >= NM: eb[i, k] = d[m].mean()
        for b in range(NB):
            for k in range(NBB):
                m = sel & (rb == b) & (bb == k)
                if m.sum() >= NM: e2[i, b, k] = (f[m] + d[m]).mean()
    return ef, eb, e2
EF_T, EB_T, E2_T = ei_tables()
def exp_income(basis=True):
    ef = EF_T[MIDX[:, None], BKT]
    if not basis: return ef
    eb = EB_T[MIDX[:, None], BBKT]; e2 = E2_T[MIDX[:, None], BKT, BBKT]
    return np.where(np.isfinite(e2), e2, ef + eb)
def gate_r7(m, Y, Hm, basis=True):
    if m == 0: return np.ones((T, N), bool)
    ei = exp_income(basis); C = entry_cost(Y, Hm)
    return np.isfinite(ei) & (ei >= m * C)

# ---------------- EV exit ratio (state-based, past-only monthly tables)
HEV = 72
REDGE = np.array([0.0, 0.0005, 0.0010, 0.0020, 0.0050]); NRB = 6                # buckets of -r8: <=0, 0-.05, .05-.1, .1-.2, .2-.5, >=.5 %
BEDGE = np.array([-0.012, -0.006, -0.003, -0.001]); NBE = 5
RB_X = np.digitize(-r8n, REDGE, right=True); BB_X = np.digitize(bnow, BEDGE)
_c = np.vstack([np.zeros((1, N)), np.cumsum((SIG | np.roll(SIG, 1, 0) | np.roll(SIG, 2, 0)).astype(np.int32), axis=0)])
def _near(wh=72):
    s = np.zeros((T, N), bool); c = np.cumsum(SIG.astype(np.int32), axis=0); c = np.vstack([np.zeros((1, N), np.int64), c])
    s[:] = (c[1:] - c[np.maximum(np.arange(T) + 1 - wh, 0)]) > 0; return s
NEAR = _near()
_e = np.minimum(np.arange(T)[:, None] + HEV, T - 1)
FX = -(np.take_along_axis(cev, np.broadcast_to(_e, (T, N)), axis=0) - cev)
BXC = np.take_along_axis(bnow, np.broadcast_to(_e, (T, N)), axis=0) - bnow
FINX = (np.arange(T)[:, None] + HEV) <= T - 1
def ev_tables(NM=200):
    ef = np.full((len(MSTART), NRB), np.nan); eb = np.full((len(MSTART), NBE), np.nan)
    for i, M in enumerate(MSTART):
        lo = max(0, M - LOOK); hi = max(lo, M - HEV)
        sel = NEAR[lo:hi] & np.isfinite(bnow[lo:hi]) & np.isfinite(BXC[lo:hi])
        for b in range(NRB):
            m = sel & (RB_X[lo:hi] == b)
            if m.sum() >= NM: ef[i, b] = FX[lo:hi][m].mean()
        for k in range(NBE):
            m = sel & (BB_X[lo:hi] == k)
            if m.sum() >= NM: eb[i, k] = BXC[lo:hi][m].mean()
    return ef, eb
EVF_T, EVB_T = ev_tables()
def ev_ratio(Y, Hm, basis=True):
    ev_ = EVF_T[MIDX[:, None], RB_X]
    if basis: ev_ = ev_ + np.nan_to_num(EVB_T[MIDX[:, None], BB_X])
    return ev_ / close_cost(Y, Hm)
