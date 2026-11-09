"""Serialize deterministic r7 tables and public cost calibration, no private caps."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,sys
import numpy as np
import pandas as pd
from bootstrap import load,ROOT,DATA,FROZEN

def finite(x):return float(x) if np.isfinite(x) else None
def table(values,mask,end,start_ms,hour=3600000):
    n=int(mask.sum())
    return {'n':n,'mean':finite(values[mask].mean()) if n>=200 else None,
        'max_outcome_end_ms':int(start_ms+end[mask].max()*hour) if n else None}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data',default=str(DATA));ap.add_argument('--output')
    args=ap.parse_args();data=Path(args.data).resolve()
    target=Path(args.output).resolve() if args.output else ROOT/'runtime/models/r7-model-bundle.json'
    if target.exists():raise FileExistsError('Frozen model exists; use --output with a new artifact path')
    D,R7,RR,RA,RS,manifest=load(data);T,N=RS.ev.shape
    start_ms=int(pd.Timestamp(manifest['start_utc'],tz='UTC').timestamp()*1000)
    # Original anchor and all monthly starts, including November 2026. This is
    # deterministic r7 updating, not a classifier or hyperparameter retrain.
    data_end=pd.Timestamp(manifest['end_utc_exclusive'],tz='UTC')
    last_refresh=data_end.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    first_refresh=pd.Timestamp(start_ms+RA.A0*3600000,unit='ms',tz='UTC')+pd.offsets.MonthBegin(1)
    updates=sorted(set([RA.A0]+[RA.HABS(p) for p in pd.date_range(first_refresh,last_refresh,freq='MS')]))
    models=[];legacy_errors=[]
    for M in updates:
        lo=max(0,M-90*24);sel=RA.SIG[lo:M]&R7.FIN24[lo:M]&(R7.END24[lo:M]<M)&np.isfinite(R7.DB24[lo:M])
        f=R7.F24[lo:M];b=R7.DB24[lo:M];end=R7.END24[lo:M];fb=RA.BKT[lo:M];bb=R7.BBKT[lo:M]
        funding=[table(f,sel&(fb==j),end,start_ms) for j in range(4)]
        basis=[table(b,sel&(bb==j),end,start_ms) for j in range(3)]
        joint=[[table(f+b,sel&(fb==j)&(bb==k),end,start_ms) for k in range(3)] for j in range(4)]
        when=pd.Timestamp(start_ms+M*3600000,unit='ms',tz='UTC');until=when+pd.offsets.MonthBegin(1)
        item={'valid_from_ms':int(when.timestamp()*1000),'valid_until_ms':int(until.timestamp()*1000),
              'sample_start_ms':start_ms+lo*3600000,'funding':funding,'basis':basis,'joint':joint}
        models.append(item)
        if M in RA.MSTART:
            ix=RA.MSTART.index(M)
            for vals,expected in [(funding,R7.EF_T[ix]),(basis,R7.EB_T[ix])]:
                for v,e in zip(vals,expected):
                    assert (v['mean'] is None)==bool(np.isnan(e))
                    if v['mean'] is not None:legacy_errors.append(abs(v['mean']-e))
            for j in range(4):
                for k in range(3):
                    v=joint[j][k]['mean'];e=R7.E2_T[ix,j,k];assert (v is None)==bool(np.isnan(e))
                    if v is not None:legacy_errors.append(abs(v-e))
    ag=pd.read_csv(data/'calib/ag.csv');cal=[];priors={}
    for n,row in D.meta.iterrows():
        priors[row.fut]={'spot_symbol':row.spot,'tokens_per_perp_quantity':str(row.mult),
            'half_spread_spot':float(row.hs_s),'half_spread_perp':float(row.hs_f),
            'spread_provenance':str(row.get('spread_source','legacy scalar prior'))}
    for row in ag.to_dict('records'):
        if row['sym'] not in priors:continue
        available=int((pd.Timestamp(row['date'],tz='UTC')+pd.Timedelta(hours=48)).timestamp()*1000)
        cal.append({'symbol':row['sym'],'sample_day':row['date'],'available_at_ms':available,
                    'y_kyle':finite(row['y_kyle']),'half_spread_perp':finite(row['hs'])})
    stamps=datetime.now(timezone.utc).isoformat()
    bundle={'format_version':'1.0.0','model_family':'frozen_r7_monthly_means_and_kyle_cost',
        'published_at_ms':int(datetime.now(timezone.utc).timestamp()*1000),'created_at_utc':stamps,
        'research_history_available_through_ms':int(pd.Timestamp(manifest['end_utc_exclusive'],tz='UTC').timestamp()*1000),
        'historical_availability_is_proxy':True,'historical_calibration_lag_hours':48,
        'income_training_rate_threshold_r8':'-0.0005','income_training_basis_max':'-0.003',
        'sample_exit_confirmation_hours':24,'sample_max_holding_hours':168,'sample_minimum':200,
        'funding_bucket_edges':[.0005,.001,.002,.005],'basis_bucket_edges':[-.012,-.006],
        'income_models':models,'asset_priors':priors,'calibration':cal,
        'cost_reference_notional_ceiling_usdt':'10000000',
        'cost_calibration_last_day':str(ag.date.max()),'cost_formula':'0.003+4*Y*sigma*sqrt(min(0.03*DV/24,10M)/max(DV,1))+2*(hs_s+hs_f)*H',
        'limitations':['Scalar half-spread priors and public trade calibration are not live order books.',
             'Historic receive times are proxies; historical use requires explicit replay mode.',
             'No model is carried into a missing new month; update requires new versioned data artifact.'],
        'data_hashes':manifest['hashes'],
        'source_hashes':{str(p.relative_to(FROZEN)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [FROZEN/'repro/lib/r7_lib.py',FROZEN/'repro/lib/run_adapt.py',FROZEN/'research/causal_inputs.py']}}
    raw=json.dumps(bundle,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    out={'bundle':bundle,'bundle_sha256':hashlib.sha256(raw).hexdigest()}
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    audit={'status':'PASS','monthly_models_exported':len(models),'old_update_dates_compared':len(RA.MSTART),
        'maximum_legacy_mean_error':max(legacy_errors,default=0),'public_calibration_rows':len(cal),
        'cost_calibration_last_day':bundle['cost_calibration_last_day'],'new_month':str(pd.Timestamp(models[-1]['valid_from_ms'],unit='ms',tz='UTC')),
        'private_inventory_included':False,'classifier_retrained':False,'bundle_sha256':out['bundle_sha256']}
    audit_path=target.with_suffix('.validation.json') if args.output else ROOT/'audit/MODEL_EXPORT_VALIDATION.json'
    audit_path.write_text(json.dumps(audit,indent=2)+'\n')
    print(json.dumps(audit),flush=True)

if __name__=='__main__':main()
