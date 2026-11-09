"""Causal settled-rate policy; forecast variants require explicit selection."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import numpy as np
import pandas as pd
from config import DATA_START

HOUR_MS = 3_600_000
ENTRY_THRESHOLD_R8 = .001
MAX_QUOTE_AGE_MS = 10_000


@dataclass
class EntryPolicy:
    r8: np.ndarray
    mask: np.ndarray
    metadata: dict


def settled_entry_policy(ev, ivh, add_timing='qualified'):
    """Use only settlements already observed, including zero-rate events.

    Historical next-settlement announcements are unavailable. The next time is
    modelled as last settlement + its interval, never by looking at future events.
    This approximation can miss a newly announced change of settlement interval.
    """
    if add_timing not in ('qualified', 'recheck'):
        raise ValueError('add_timing must be qualified or recheck')
    ev = np.asarray(ev, dtype=float); ivh = np.asarray(ivh, dtype=float)
    if ev.ndim != 2 or ev.shape != ivh.shape:
        raise ValueError('settled rates and explicit event intervals must have the same 2D shape')
    events = ivh > 0
    if not np.isfinite(ivh).all() or (ivh < 0).any():
        raise ValueError('settlement intervals must be finite and nonnegative')
    if not np.isfinite(ev[events]).all() or ((ev != 0) & ~events & np.isfinite(ev)).any():
        raise ValueError('each nonzero settled rate requires an explicit event interval')
    if (events & (ivh != np.floor(ivh))).any():
        raise ValueError('hourly replay requires whole-hour settlement intervals')
    T, N = ev.shape
    r8 = np.full((T, N), np.nan); mask = np.zeros((T, N), dtype=bool)
    latest = np.full(N, np.nan); next_at = np.full(N, -1.)
    armed_at = np.full(N, -1, dtype=np.int64)
    for t in range(T):
        m = events[t]
        latest[m] = ev[t, m] * 8. / ivh[t, m]
        next_at[m] = t + ivh[t, m]
        qualified = latest < -ENTRY_THRESHOLD_R8
        armed_at[~qualified] = -1
        new_signal = qualified & (armed_at < 0)
        armed_at[new_signal] = t
        r8[t] = latest
        # Do not enter immediately when a 1h contract's signal and T-1h coincide.
        mask[t] = qualified & (armed_at < t) & (next_at == t + 1)
    metadata = dict(name='latest_settled_strict_below_minus_0.1pct_8h_at_T_minus_1h',
                    signal_source='settled', reconstructed_forecasts=False,
                    threshold_r8=-ENTRY_THRESHOLD_R8, comparator='<', recheck_before_hours=1,
                    applies_to=['entry'] if add_timing == 'qualified' else ['entry', 'add'],
                    add_timing=add_timing, exit_restricted_to_recheck=False,
                    add_rule='any qualified hour while held' if add_timing == 'qualified' else 'T-1h recheck only',
                    income_training_sample_scope='T-1h recheck hours; frozen across add-timing comparison',
                    interval_universe='all observed intervals including 1h, 4h, 8h',
                    schedule_model='last settled interval persists; no future settlement lookup',
                    historical_announced_schedule_available=False,
                    first_signal_action='arm only; no immediate entry',
                    recheck_quotes=int(mask.sum()),
                    below_threshold_quotes=int((r8 < -ENTRY_THRESHOLD_R8).sum()),
                    extra_adl_filter=False, expected_exit_confirmation_hours=1)
    return EntryPolicy(r8, mask, metadata)


def load_entry_policy(path, shape, symbols, signal_source='archived'):
    """Each row is information available at the start of that dataset hour.

    Missing or stale quotes never carry forward. The announced next settlement
    time is read from the same snapshot, never inferred from future `ev`/`ivh`.
    Use the last quote captured before the decision, at most ten seconds old.
    Requiring an exact timestamp would reject real asynchronous market feeds.
    Reconstructed inputs use availability timestamps and modelled schedules;
    their provenance is preserved and requires explicit caller opt-in.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing pre-settlement snapshots: {path}. Need the forecast rate, "
            "announced interval and next settlement time observed at T-1h; "
            "settled funding cannot reconstruct this entry rule.")
    fields = ('observed_at_ms', 'next_settlement_at_ms',
              'expected_funding_rate', 'funding_interval_hours')
    with np.load(path, allow_pickle=False) as z:
        stored_source = str(z['signal_source'].item()) if 'signal_source' in z.files else 'archived'
        if signal_source not in ('archived', 'reconstructed') or stored_source != signal_source:
            raise ValueError(f'entry signal source is {stored_source}; explicitly select the matching --signal-source')
        method = str(z['reconstruction_method'].item()) if 'reconstruction_method' in z.files else None
        required = {'symbols', 'decision_time_ms', *fields}
        if not required.issubset(z.files):
            raise ValueError('missing entry snapshot fields: ' + ', '.join(sorted(required-set(z.files))))
        if z['symbols'].shape != (shape[1],) or z['symbols'].tolist() != list(symbols):
            raise ValueError('entry snapshot symbol order must match dataset metadata')
        start_ms = pd.Timestamp(DATA_START, tz='UTC').value // 1_000_000
        decision = start_ms + np.arange(shape[0], dtype=np.int64)*HOUR_MS
        if not np.array_equal(z['decision_time_ms'], decision):
            raise ValueError('entry snapshot time grid must match dataset hours')
        arrays = {key: z[key].astype(np.float64) for key in fields}
    for key, arr in arrays.items():
        if arr.shape != tuple(shape):
            raise ValueError(f'entry snapshot {key} must have shape {tuple(shape)}')
        if np.isinf(arr).any():
            raise ValueError(f'entry snapshot {key} contains infinity')
    observed = arrays['observed_at_ms']; nxt = arrays['next_settlement_at_ms']
    rate = arrays['expected_funding_rate']; interval = arrays['funding_interval_hours']
    if (np.isfinite(observed) & (observed > decision[:, None])).any():
        raise ValueError('entry snapshot observed after decision time (look-ahead)')
    if (np.isfinite(interval) & (interval <= 0)).any():
        raise ValueError('entry snapshot funding interval must be positive')
    age = decision[:, None] - observed
    mask = ((age >= 0) & (age <= MAX_QUOTE_AGE_MS)
            & (nxt == decision[:, None] + HOUR_MS)
            & np.isfinite(rate) & np.isfinite(interval) & (interval > 0))
    r8 = np.full(shape, np.nan)
    r8[mask] = rate[mask] * 8. / interval[mask]
    if not np.isfinite(r8[mask]).all():
        raise ValueError('entry snapshot normalised rate is not finite')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    metadata = dict(name='forecast_strict_below_minus_0.1pct_8h_at_T_minus_1h',
                    signal_source=stored_source, reconstructed_forecasts=stored_source == 'reconstructed',
                    reconstruction_method=method,
                    threshold_r8=-ENTRY_THRESHOLD_R8, comparator='<', recheck_before_hours=1,
                    max_quote_age_ms=MAX_QUOTE_AGE_MS,
                    applies_to=['entry', 'add'], exit_restricted_to_recheck=False,
                    snapshot_sha256=digest, recheck_quotes=int(mask.sum()),
                    below_threshold_quotes=int((r8 < -ENTRY_THRESHOLD_R8).sum()),
                    missing_quotes='no entry; no settled-rate fallback')
    return EntryPolicy(r8, mask, metadata)
