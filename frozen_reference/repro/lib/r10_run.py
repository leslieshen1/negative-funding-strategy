"""Round 10: exit-confirmation window + two-stage exit on the conservative config.  Pre-committed candidate set (6): confirm window 0/12/24/48h and two-stage x2a (50% at once when the exit condition triggers, rest after 24h persistence), x2b (50% after 24h, rest after 48h).
Config name = cons[_c<h>|_x2a|_x2b][_k<k>]; frozen reference = cons (conf_h=48).  S from env NFB_S.  Aggregates only."""
import sys, os, time; ARGV = list(sys.argv); sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import r9_run as R9
from r9_run import *
_old = R9.getfix
def getfix10(name):
    t = name.split("_"); ex = {}; rest = [t[0]]
    for x in t[1:]:
        if x[0] == "c" and x[1:].isdigit(): ex["conf_h"] = float(max(1, int(x[1:])))          # 0 -> 1 = no confirmation (exit as soon as the condition holds)
        elif x == "cns": ex["conf_h"] = 999.0                                                 # r11: next-settlement confirmation
        elif x == "x2a": ex.update(x2_frac=0.5, x2_conf1=1.0, conf_h=24.0)
        elif x == "u1": ex["res_mode"] = 1.0                                                  # residual unwound pov-limited (reports unwind days)
        elif x == "x2b": ex.update(x2_frac=0.5, x2_conf1=24.0, conf_h=48.0)
        else: rest.append(x)
    if rest[0] == "allC":            # optimistic bound (fx_fund=1) + optional k / r / delay tokens
        f = _old("allC")
        for x in rest[1:]:
            if x[0] == "k": f["k_imp"] = float(x[1:])
            elif x[0] == "r": f["res_mult"] = float(x[1:])
            elif x == "dS": f["leg_dly"] = 1.0
            else: raise ValueError(name)
    else: f = _old("_".join(rest))
    f.update(ex); return f
R9.getfix = getfix10
if __name__ == "__main__":
    fixes = ARGV[1].split(","); wins = ARGV[2].split(","); nw = int(ARGV[3]); tag = ARGV[4]
    jobs = [(f, w) for f in fixes for w in wins]
    with mp.get_context("fork").Pool(nw) as p: rows = p.map(R9.job9, jobs, chunksize=1)
    df = pd.DataFrame(rows); df.to_csv(OUT + f"r10_{tag}_S{int(S/1e6)}M.csv", index=False)
    print(df[["fix", "win", "ann", "net_u", "episodes", "avg_hold_h", "held_hours", "sec"]].round(2).to_string())
