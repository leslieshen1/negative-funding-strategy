"""Aggregate concentration, settlement-interval and chronological evidence.

Never exports borrowed quantities or symbol-by-hour positions.
"""
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd
import acquire_phase1 as A


def audit(directory, close, interval):
    source = directory/'private/reference_positions.npz'
    with np.load(source) as z:
        qty, start, symbols = z['qty_token'], int(z['start_hour']), z['symbols']
    prices = close[start:start+len(qty)]
    notional = qty*prices
    t, n = np.unravel_index(np.argmax(notional), notional.shape)
    shares = {}
    iv = interval[start:start+len(qty)]
    total = notional.sum()
    for hours in (1, 2, 4, 8):
        shares[str(hours)+'h'] = float(notional[np.isclose(iv, hours, atol=.01)].sum()/total) if total else 0.
    over = notional > 1_000_000.01
    summary = dict(position_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                   maximum_single_coin_mark_usdt=float(notional[t, n]), symbol_at_maximum=str(symbols[n]),
                   maximum_at_utc=(A.START+pd.Timedelta(hours=start+int(t))).isoformat(),
                   single_coin_mark_over_1m_coin_hours=int(over.sum()),
                   maximum_overshoot_above_1m_usdt=float(max(0., notional.max()-1e6)),
                   note='Prior-close target; price jumps and shared participation constraints can leave temporary overshoots. This is not an instantaneous hard mark cap.',
                   held_notional_hour_share_by_latest_settled_interval=shares,
                   interval_shares_are_not_PnL_attribution=True,
                   private_quantities_exported=False)
    A.js(directory/'concentration_audit.json', summary)
    curve = pd.read_csv(directory/'causal_reference_FY_curve.csv')
    change = np.diff(np.r_[0., curve.pnl_usdt.to_numpy()])
    curve['pnl_change_usdt'] = change
    curve['month_utc'] = pd.to_datetime(curve.hour_utc, utc=True).dt.strftime('%Y-%m')
    curve.groupby('month_utc', sort=True).agg(pnl_usdt=('pnl_change_usdt', 'sum'),
        mean_mark_position_usdt=('position_mtm_usdt', 'mean'), hours=('pnl_change_usdt', 'size')).to_csv(directory/'monthly_pnl.csv')
    return summary


def main():
    with np.load(A.ROOT/'data/ds/dataset.npz') as z:
        close = pd.DataFrame(z['sc']).where(lambda x: x > 0).ffill().fillna(0.).to_numpy()
        interval = pd.DataFrame(np.where(z['ivh'] > 0, z['ivh'], np.nan)).ffill().to_numpy()
    summaries = {}
    for name in ['candidate_balanced', 'candidate_lower_oi', 'concentration']:
        summaries[name] = audit(A.ROOT/name, close, interval)
    reference = pd.read_csv(A.ROOT/'lifecycle_verified/causal_reference_FY_curve.csv').pnl_usdt.to_numpy()
    candidate = pd.read_csv(A.ROOT/'concentration/causal_reference_FY_curve.csv').pnl_usdt.to_numpy()
    difference = np.diff(np.r_[0., candidate])[:8760]-np.diff(np.r_[0., reference])[:8760]
    blocks = np.array([difference[t:t+168].sum() for t in range(0, 8760, 168)])
    draws = np.random.default_rng(20261106).integers(0, len(blocks), (2000, len(blocks)))
    bootstrap = blocks[draws].sum(axis=1)/1e7*100
    A.js(A.ROOT/'PORTFOLIO_AUDIT.json', dict(candidates=summaries,
        diversified_minus_liquidity_reference_common_8760h=dict(
            difference_percentage_points=float(difference.sum()/1e7*100),
            exploratory_weekly_block_95pct_interval=list(map(float, np.quantile(bootstrap, [.025, .975]))),
            previously_seen_year=True, multiple_testing_adjusted=False),
        actual_execution_verified=False))


if __name__ == '__main__':
    main()
