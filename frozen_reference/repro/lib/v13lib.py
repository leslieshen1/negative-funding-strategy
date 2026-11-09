"""v13 helper: headline engine (cons, k=4, 0h) with extra entry mask / parameter overrides. Nothing in the engine is changed.
Usage: ARGV must be captured by the caller before importing (run_adapt overwrites sys.argv)."""
import os, sys
V13 = "."
os.environ.setdefault("NFB_OUT", V13 + "/out/")
os.environ.setdefault("NFB_DAILY", V13 + "/out/daily/")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import r10_run as R10                      # patches r9_run.getfix with the c0 / k tokens
import r9_run as R9
from r9_run import *                       # D, N, T, S, np, pd, mp, RR, OUT ...
import run_r7 as RR
import lib12s as L
from run_adapt import A0, A_IS, A1, HABS
from run_signals import sum24, ADL_MASK, r8n, fc, sc
import r7_lib as R7

EXTRA_OK = {"mask": None}                  # (T,N) bool AND-ed into ok_t
_orig_make_ok = RR.make_ok
def make_ok13(gate_spec, scen):
    ok = _orig_make_ok(gate_spec, scen)
    return ok if EXTRA_OK["mask"] is None else (ok & EXTRA_OK["mask"])
RR.make_ok = make_ok13

# --- copy of lib12s.run with two extra knobs: oi_frac (OI cap fraction, headline 0.10) and vol_cap_frac scale handled via engine's vol_frac param
OIF = {"v": 0.10}
_src = open(L.__file__).read()
_src = _src.replace("oilim = np.where(np.isfinite(oi), 0.10 * oi, 0.0)", "oilim = np.where(np.isfinite(oi), OIF['v'] * oi, 0.0)")
_ns = dict(L.__dict__); _ns["OIF"] = OIF
exec(compile(_src.split("def run(D, S")[1].join(["def run(D, S", ""]) if False else _src, "lib12s_v13", "exec"), _ns)
RR.run = _ns["run"]

def run_fix(fix, win, mask=None, extra=None, oi_frac=0.10, m=2.0):
    """fix = r10 token string e.g. 'cons_c0' / 'cons_c0_k6'; win in IS/HO/FY; extra = dict of engine param overrides (pov, vol_frac, cool_h, ...)."""
    EXTRA_OK["mask"] = mask; OIF["v"] = oi_frac
    old = R9.getfix
    if extra:
        def g(name, _o=old): f = _o(name); f.update(extra); return f
        R9.getfix = g
    try:
        f = R9.getfix(fix); ex = dict(conf_h=48.0); rk = {}
        if f.get("f_coin", 0) > 0.5: rk["capq"] = QTY
        ex.update(f)
        r = RR.run_cfg(("r7", m), ex, "cal", win, True, rkw=rk)
    finally: R9.getfix = old; EXTRA_OK["mask"] = None; OIF["v"] = 0.10
    return r

def ann_of(r, win=None):
    cum = np.concatenate([[0.0], r["cum"]]); H = len(cum) - 1
    return cum[-1] / S / (H / 8760.0) * 100

def hourly(r):
    cum = np.concatenate([[0.0], r["cum"]]); return np.diff(cum)
