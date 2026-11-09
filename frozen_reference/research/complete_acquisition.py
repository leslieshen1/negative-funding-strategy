"""Complete mark history and the fixed-calendar extra-symbol calibration.

Run after acquire_phase1 and before build_phase1. The old 315-cohort calibration
and legacy market data are explicit dependencies, not silently reconstructed.
"""
from datetime import date, timedelta
import concurrent.futures as cf
import pandas as pd
import acquire_phase1 as A
import calibration_stream as C


def main():
    universe = A.universe()
    jobs = []
    for row in universe['pair_candidates']:
        if not row['prior_315']:
            continue
        symbol = row['fut']
        for month in pd.date_range('2025-10-01', '2026-10-01', freq='MS'):
            tag = month.strftime('%Y-%m')
            jobs.append(('mark', symbol, tag,
                         f'{A.BASE}/futures/um/monthly/markPriceKlines/{symbol}/1h/{symbol}-1h-{tag}.zip'))
    A.batch(jobs, 'complete_old_cohort_marks')
    days = []
    day = date(2025, 10, 1)
    while day <= date(2026, 10, 21):
        days.append(day.isoformat())
        day += timedelta(days=28)
    extra = [r['fut'] for r in universe['pair_candidates'] if not r['prior_315']]
    jobs = [(symbol, day) for symbol in extra for day in days]
    with cf.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(C.fetch_reduce, jobs))
    errors = [r for r in results if r['status'] not in ('ok', 'unavailable_404')]
    if errors:
        raise RuntimeError('Calibration failures retained in records; retry after inspection')
    rows = [dict(sym=r['sym'], date=r['date'], **r['values'])
            for r in results if r['status'] == 'ok']
    original = pd.read_csv(A.PRIOR/'calib/ag.csv')
    combined = pd.concat([original, pd.DataFrame(rows)], ignore_index=True).sort_values(['date', 'sym'])
    if combined.duplicated(['date', 'sym']).any():
        raise ValueError('Duplicate calibration day')
    combined.to_csv(A.ROOT/'calib/ag.csv', index=False)
    A.js(A.ROOT/'extra_calibration_summary.json',
         dict(jobs=len(jobs), ok=len(rows), unavailable_404=sum(r['status'] == 'unavailable_404' for r in results),
              errors=0, combined_rows=len(combined), same_original_calendar=True))


if __name__ == '__main__':
    main()
