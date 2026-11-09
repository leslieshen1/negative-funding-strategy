"""Compare new scalar r7 against untouched vector research code on real history."""
from pathlib import Path
from collections import Counter
import json,sys
import numpy as np
import pandas as pd
from bootstrap import load,ROOT,DATA
from market_request import model_row,full_request,value

def main():
    D,R7,RR,RA,RS,manifest=load()
    # Frozen reference calculations remain independent from the scalar port.
    source_income=R7.exp_income(True);source_cost=R7.entry_cost(RR.YK,RR.HM)
    start_ms=int(pd.Timestamp(manifest['start_utc'],tz='UTC').timestamp()*1000)
    with np.load(DATA/'ds/dataset.npz') as z:ivh=z['ivh'].copy()
    sys.path.insert(0,str(ROOT/'runtime'))
    from online_scorer import Scorer
    from decision import decide
    from validate import validate_pair,strict_load
    scorer=Scorer.from_path(ROOT/'runtime/models/r7-model-bundle.json')
    # Include weak/no-entry conditions as well as eligible negative signals.
    h=np.arange(len(D.arr[0]))[:,None]
    valid=(h>=RA.A0)&(h<RA.A1)&np.isfinite(D.arr[1])&(D.arr[1]>0)&(D.arr[4]>0)
    strong=np.argwhere(valid&(RS.r8n<-.001)&(RA.b_prev<=-.003))
    other=np.argwhere(valid&~((RS.r8n<-.001)&(RA.b_prev<=-.003)))
    rng=np.random.default_rng(20261108)
    selected=np.vstack([strong[rng.choice(len(strong),min(4000,len(strong)),replace=False)],other[rng.choice(len(other),min(2000,len(other)),replace=False)]])
    reasons=Counter();checked=0;gate_mismatch=0;adl_mismatch=0;maximums=Counter();entries=[]
    for t,n in selected:
        t=int(t);n=int(n);now=start_ms+t*3600000
        row=model_row(D,ivh,t,n,start_ms);score,why=scorer.score(row,now,replay=True)
        if score['status']!='VALID':reasons.update(why);continue
        if not np.isfinite(source_income[t,n]):raise AssertionError('new score exists where legacy income is missing')
        errors={'income':abs(float(score['expected_income_rate'])-source_income[t,n]),
                'cost':abs(float(score['round_trip_cost_rate'])-source_cost[t,n]),
                'sigma':abs(float(score['sigma_24h'])-D._sg[t,n]),
                'y':abs(float(score['calibration_y'])-RR.YK[t,n]),
                'spread_multiplier':abs(float(score['calibration_spread_multiplier'])-RR.HM[t,n])}
        for k,e in errors.items():maximums[k]=max(maximums[k],float(e))
        if max(errors.values())>1e-10:raise AssertionError((t,n,errors))
        gate_mismatch+=score['r7_gate_passed']!=bool(source_income[t,n]>=2*source_cost[t,n])
        adl_mismatch+=score['adl_entry_blocked']!=bool(RS.ADL_MASK[t,n]);checked+=1
        if score['r7_gate_passed'] and RS.r8n[t,n]<-.001 and RA.b_prev[t,n]<=-.003 and not RS.ADL_MASK[t,n] and np.isfinite(D.oih[t-1,n]) and D.oih[t-1,n]>0:
            entries.append((t,n))
    assert checked>1000 and gate_mismatch==adl_mismatch==0,(checked,gate_mismatch,adl_mismatch)
    template=strict_load(ROOT/'runtime/examples/01_enter.input.json')
    e2e=[]
    for t,n in entries[:100]:
        req=full_request(template,D,ivh,t,n,start_ms)
        result=decide(req,scorer,now_ms=req['as_of_ms'],replay=True)
        validate_pair(req,result,req['as_of_ms'])
        if not result['targets'][0]['legs']:continue
        e2e.append({'as_of_ms':req['as_of_ms'],'symbol':D.fut[n],'action':result['targets'][0]['action']})
        if len(e2e)<=3:
            dst=ROOT/'audit/private'/f'real-market-synthetic-account-{len(e2e)}'
            dst.with_suffix('.input.json').write_text(json.dumps(req,indent=2)+'\n')
            dst.with_suffix('.output.json').write_text(json.dumps(result,indent=2)+'\n')
    assert len(e2e)>20,len(e2e)
    result={'status':'PASS','selected_historical_coin_hours':len(selected),'valid_scores_compared':checked,
        'unavailable_reasons':dict(reasons),'maximum_absolute_errors':dict(maximums),'income_cost_gate_mismatches':gate_mismatch,
        'adl_proxy_mismatches':adl_mismatch,'end_to_end_decisions_with_actual_historical_market_and_synthetic_accounts':len(e2e),
        'signal_or_formula_equivalence_only':True,'historical_execution_filter_equivalence_verified':False,
        'actual_execution_verified':False,'previously_seen_history':True,'new_live_return_claim':False}
    (ROOT/'audit/R7_POINT_IN_TIME_PARITY.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
