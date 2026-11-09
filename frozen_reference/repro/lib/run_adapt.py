"""Round 4: rolling past-only entry gate + exit confirmation + same-hour caps, IS-only selection, single holdout run. Aggregates only."""
import sys; sys.argv = ["x", "none"]
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from run_signals import *
import multiprocessing as mp, json

T0 = pd.Timestamp(C.DATA_START, tz="UTC")
def HABS(d):
    p = pd.Timestamp(d); p = p.tz_localize("UTC") if p.tzinfo is None else p
    return int((p - T0).total_seconds() // 3600)
A0, A_IS, A1 = h0, h0 + H_IS, h1                                   # absolute hour indices: window start, IS end / holdout start, window end
assert HABS("2025-10-28") == A0
EDGES = np.array([0.0005, 0.0010, 0.0020, 0.0050]); NB = 4; NMIN = 200; LOOK = 90 * 24; HOR = 168
b_prev = shift((fc - sc) / np.where(sc > 0, sc, np.nan), 1)         # basis seen at t-1, as in the engine entry
SIG = (r8n < -0.0005) & np.isfinite(b_prev) & (b_prev <= -0.003) & ~ADL_MASK
BKT = np.digitize(-r8n, EDGES) - 1                                   # 0..3 for -r8 >= 0.05%
BKT = np.clip(BKT, 0, NB - 1)
cev = np.cumsum(ev, axis=0)
HSC = float(np.nanmedian(D.hs_s) + np.nanmedian(D.hs_f))
def cost_C(imp_bp): return 0.0030 + 4 * imp_bp / 1e4 + 2 * HSC
MSTART = [A0] + [HABS(p) for p in pd.date_range("2025-11-01", "2026-10-01", freq="MS", tz="UTC")]
MSTART = sorted(set(MSTART))

def sample_outcomes(conf):
    """F[t,n] = funding collected over t+1..end, end = min(first confirmed X5 exit, t+168); end[t,n] the hour at which the outcome is fully known."""
    B = (sum24 > 0)
    W = np.zeros((T, N), bool)
    if conf == 1: W = B.copy()
    else:
        c = np.cumsum(np.vstack([np.zeros((1, N)), B.astype(np.int32)]), axis=0)      # c[k] = sum B[:k]
        W[conf - 1:] = (c[conf:T + 1] - c[:T - conf + 1]) == conf
    nxt = np.full((T + conf + 1, N), 10**9, dtype=np.int64)
    for u in range(T - 1, -1, -1): nxt[u] = np.where(W[u], u, nxt[u + 1])
    tt = np.arange(T)[:, None]
    ex = nxt[np.minimum(np.arange(T) + conf, T + conf)]
    end = np.minimum(ex, tt + HOR).astype(np.int64)
    fin = end <= T - 1
    endc = np.minimum(end, T - 1)
    F = -(np.take_along_axis(cev, endc, axis=0) - cev)
    return F, end, fin

def ef_table(conf):
    """EF[m, bucket] known at month start MSTART[m]: only samples whose outcome is complete before that hour, started within 90d."""
    F, end, fin = sample_outcomes(conf); tab = np.full((len(MSTART), NB), np.nan); cnt = np.zeros((len(MSTART), NB), int)
    for i, M in enumerate(MSTART):
        lo = max(0, M - LOOK)
        sel = SIG[lo:M] & fin[lo:M] & (end[lo:M] < M)
        f = F[lo:M]; bk = BKT[lo:M]
        for b in range(NB):
            m = sel & (bk == b); cnt[i, b] = m.sum()
            if cnt[i, b] >= NMIN: tab[i, b] = f[m].mean()
    return tab, cnt

EFT = {c: ef_table(c) for c in (1, 6, 12, 24)}
MIDX = np.zeros(T, int)
for i, M in enumerate(MSTART): MIDX[M:] = i
def gate(m, conf, imp_bp=30.0):
    if m == 0: return np.ones((T, N), bool)
    tab = EFT[conf][0]; ef = tab[MIDX[:, None], BKT]                # (T,N), nan where bucket not estimable
    return np.isfinite(ef) & (ef >= m * cost_C(imp_bp))

def arun(m, conf, hw, imp=30, extra=None, same=True, rule="adapt"):
    ok = ~ADL_MASK & gate(m, conf) if rule == "adapt" else ~ADL_MASK
    ex = dict(ENTRY["E7"]); ex.update(EXIT["X5"]); ex.update(FIXED); ex["x_obs"] = 1.0
    if conf > 1: ex["conf_h"] = float(conf)
    if extra: ex.update(extra)
    return run(D, S, ok_t=ok, extra=ex, imp=imp, dl_stop=False, advmode=1.0, s24=sum24, hw=hw, same_hour=same, **MODES["i"])

def summ_win(r, hw):
    cum = np.concatenate([[0.0], r["cum"]]); H = len(cum) - 1; y = H / 8760.0; ep = r["ep"]
    out = dict(net_u=cum[-1], ann=cum[-1] / S / y * 100, episodes=int(r["episodes"]))
    if len(ep):
        dur = ep[:, 2] - ep[:, 1]; net = ep[:, 3] - ep[:, 6]
        for nm, lo, hi in (("lt24", 0, 24), ("d24_72", 24, 72), ("d72_168", 72, 168), ("gt168", 168, 1e18)):
            mm = (dur >= lo) & (dur < hi); out[f"n_{nm}"] = int(mm.sum()); out[f"net_{nm}_u"] = float(net[mm].sum())
        cp = np.bincount(ep[:, 0].astype(int), weights=net, minlength=N); top = np.sort(cp)[::-1][:5]
        out["ex5_u"] = float(cp.sum() - top.sum()); out["ex5_ann"] = out["ex5_u"] / S / y * 100; out["top5_share"] = float(top.sum() / cp.sum() * 100) if cp.sum() != 0 else np.nan
        out["avg_hold_h"] = float(dur.mean())
    dg = r["diag"]; out["avg_cost_used_u"] = dg[15] / H; out["peak_cost_used_u"] = dg[12]
    xx = r["xd"]; out["funding_u"] = None
    return out
