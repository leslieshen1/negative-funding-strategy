"""Build a new audited research input; preserve the previous input unchanged."""
from pathlib import Path
import concurrent.futures as cf,hashlib,json,time,io,zipfile
import numpy as np
import pandas as pd
import acquire_phase1 as A

ROOT=A.ROOT;START=A.START;T=A.T
def november(rec):
 s=rec['fut'];dst=ROOT/'raw/funding_api'/f'{s}.json'
 if dst.exists():
  old=json.loads(dst.read_text())
  if old['status']=='ok':return old
 params={'symbol':s,'startTime':int(pd.Timestamp('2026-11-01',tz='UTC').timestamp()*1000),
  'endTime':int(A.END.timestamp()*1000)-1,'limit':1000}
 out={'symbol':s,'params':params}
 for host in ['https://fapi.binance.com','https://www.binance.com']:
  for attempt in range(2):
   try:
    r=A.sess().get(host+'/fapi/v1/fundingRate',params=params,timeout=(8,25))
    if r.status_code in (418,429):raise RuntimeError('API rate limit; do not retry aggressively')
    r.raise_for_status();data=r.json()
    if not isinstance(data,list):raise ValueError('not a history array')
    if len(data)>=1000:raise ValueError('pagination required')
    out.update(status='ok',url=host+'/fapi/v1/fundingRate',rows=data,sha256=hashlib.sha256(r.content).hexdigest(),fetched_at_utc=pd.Timestamp.now(tz='UTC').isoformat())
    A.js(dst,out);return out
   except Exception as e:out.update(status='error',error=type(e).__name__+': '+str(e));time.sleep(.3)
 A.js(dst,out);return out

def main():
 if not (ROOT/'acquisition_summary.json').exists():raise RuntimeError('finish archive acquisition before building')
 u=A.universe();meta_old=pd.read_csv(A.PRIOR/'data/ds/dataset_meta.csv').set_index('fut')
 # The public endpoint is 500/5min shared with fundingInfo. One request/symbol,
 # under 500 total; no authenticated account endpoint is used.
 with cf.ThreadPoolExecutor(8) as pool:api=list(pool.map(november,u['pair_candidates']))
 errors=[{'symbol':r['symbol'],'error':r['error']} for r in api if r['status']!='ok']
 A.js(ROOT/'funding_api_summary.json',{'symbols':len(api),'ok':len(api)-len(errors),'errors':errors})
 if errors:raise RuntimeError('public funding API errors; explicit records retained')
 arr=A.market_arrays(u);N=len(u['pair_candidates'])
 arr.update(ev=np.zeros((T,N)),ivh=np.zeros((T,N)),r8=np.full((T,N),np.nan),r8s=np.zeros((T,N)),
  d02=np.zeros((T,N)),d1=np.zeros((T,N)))
 offset=int((pd.Timestamp('2025-10-01',tz='UTC')-START).total_seconds()/3600)
 oldmeta=pd.read_csv(A.PRIOR/'data/ds/dataset_meta.csv');lookup={s:i for i,s in enumerate(oldmeta.fut)}
 pairs=[(n,lookup[r['fut']]) for n,r in enumerate(u['pair_candidates']) if r['fut'] in lookup]
 dest_cols,source_cols=map(list,zip(*pairs))
 with np.load(A.PRIOR/'data/ds/dataset.npz') as old:
  for k in ['d02','d1']:
   source=old[k];arr[k][offset:offset+len(source),dest_cols]=source[:,source_cols]
 source_flags=[];meta=[];event_count=0;zero_count=0;proxy_marks=0
 for n,r in enumerate(u['pair_candidates']):
  s=r['fut'];events=[]
  for path in sorted((ROOT/'raw/funding'/s).glob('*.zip')):
   with zipfile.ZipFile(path) as z:f=pd.read_csv(z.open(z.namelist()[0]))
   for x in f.itertuples(index=False):events.append((int(x.calc_time),float(x.last_funding_rate),float(x.funding_interval_hours),None))
  events.sort();last_time=events[-1][0] if events else None;last_iv=events[-1][2] if events else None
  for x in sorted(api[n]['rows'],key=lambda x:x['fundingTime']):
   stamp=int(x['fundingTime']);iv=(stamp-last_time)/3600000 if last_time is not None else np.nan
   if not (0<iv<=8.05):iv=last_iv if last_iv is not None else np.nan
   if np.isfinite(iv):iv=round(iv,6)
   events.append((stamp,float(x['fundingRate']),iv,float(x['markPrice']) if x.get('markPrice') else None))
   last_time=stamp;last_iv=iv if np.isfinite(iv) else last_iv
  seen=set();occupied=set()
  for stamp,rate,iv,mark in sorted(events):
   if stamp in seen:raise ValueError('duplicate settlement timestamp '+s)
   seen.add(stamp)
   # Existing hourly model settles before placing new orders in that hour.
   # Small economic-event jitter is aligned within one minute, not labelled
   # as a historical receive timestamp or subsecond execution replay.
   h=int(np.ceil((stamp-START.value//10**6-60000)/3600000))
   if not (0<=h<T):continue
   if h in occupied:raise ValueError('multiple settlements in a single hour '+s)
   occupied.add(h);arr['ev'][h,n]=rate;event_count+=1;zero_count+=rate==0
   if np.isfinite(iv) and iv>0:arr['ivh'][h,n]=iv;arr['r8'][h,n]=rate*8/iv
   if mark is not None and mark>0:arr['mk'][h,n]=mark/r['mult']
   else:proxy_marks+=1
  arr['r8'][:,n]=pd.Series(arr['r8'][:,n]).ffill().to_numpy()
  snapshots={}
  for path in sorted((ROOT/'raw/oi'/s).glob('*.zip')):
   with zipfile.ZipFile(path) as z:f=pd.read_csv(z.open(z.namelist()[0]))
   ts=pd.to_datetime(f.create_time,utc=True).astype('int64').to_numpy()
   # pandas datetime resolution is explicit here; normalize to milliseconds.
   ts=pd.DatetimeIndex(pd.to_datetime(f.create_time,utc=True)).as_unit('ms').asi8
   vals=pd.to_numeric(f.sum_open_interest_value).to_numpy(float)
   for stamp,v in zip(ts,vals):
    if not np.isfinite(v) or v<0:raise ValueError('invalid OI')
    h=int((stamp-START.value//10**6)//3600000)+1
    if 0<=h<T and (h not in snapshots or stamp>snapshots[h][0]):snapshots[h]=(stamp,v)
  for h,(_,v) in snapshots.items():arr['oih'][h,n]=v
  # A lagged daily series is kept only for compatibility, not the main cap.
  daily=pd.Series(arr['oih'][:,n]).groupby(np.arange(T)//24).last().shift(1)
  arr['oi'][:,n]=np.repeat(daily.to_numpy(),24)[:T]
  paired=np.minimum(np.nan_to_num(arr['sq'][:,n]),np.nan_to_num(arr['fq'][:,n]))
  proxy=pd.Series(paired).rolling(24,min_periods=6).median().shift(1).fillna(0.).to_numpy()
  # Proxy magnitudes are explicit assumptions, not observed order books.
  for key,k in [('d02',.26),('d1',2.4)]:
   missing=(arr[key][:,n]<=0)|~np.isfinite(arr[key][:,n]);arr[key][missing,n]=k*proxy[missing]
  if s in meta_old.index:
   m=meta_old.loc[s].to_dict();m['fut']=s;m['spread_source']='legacy_scalar_prior_with_dynamic_observed_perp_scaling'
  else:
   m=dict(fut=s,spot=r['spot'],mult=r['mult'],hs_s=.0005,hs_f=.0005,r2=2.,r5=5.,tick_s=0.,tick_f=0.,
    dl_exit=1e9,dl_fin=1e9,delisted=False,grp='historical_extension',depth_source='volume_proxy',spread_source='explicit_5bp_halfspread_prior_not_observed')
  meta.append(m)
  active=(arr['sc'][:,n]>0)&(arr['fc'][:,n]>0)
  fy=(np.arange(T)>=int((pd.Timestamp('2025-10-28',tz='UTC')-START).total_seconds()/3600))&(np.arange(T)<int((pd.Timestamp('2026-10-28',tz='UTC')-START).total_seconds()/3600))
  source_flags.append({'symbol':s,'prior_315':r['prior_315'],'paired_hours_in_FY':int((active&fy).sum()),
   'OI_missing_while_paired_FY':int((active&fy&~np.isfinite(arr['oih'][:,n])).sum()),'settlements':len(occupied)})
 ds=ROOT/'data/ds';ds.mkdir(parents=True,exist_ok=True);np.savez_compressed(ds/'dataset.npz',**arr)
 pd.DataFrame(meta).to_csv(ds/'dataset_meta.csv',index=False)
 cal=ROOT/'data/calib';cal.mkdir(parents=True,exist_ok=True)
 for name in ['ag.csv','bd.csv']:
  target=cal/name
  source=ROOT/'calib/ag.csv' if name=='ag.csv' else A.PRIOR/'calib'/name
  if not source.exists():raise RuntimeError('finish additional calibration before building')
  if not target.exists():target.symlink_to(source)
 hashes={}
 for p in [ds/'dataset.npz',ds/'dataset_meta.csv',cal/'ag.csv',cal/'bd.csv']:
  with p.open('rb') as f:hashes[str(p.relative_to(ROOT/'data'))]=hashlib.file_digest(f,'sha256').hexdigest()
 manifest={'status':'built','start_utc':'2025-07-01','end_utc_exclusive':'2026-11-06','hours':T,'symbols':N,
  'fiscal_paired_symbols':sum(x['paired_hours_in_FY']>0 for x in source_flags),
  'additional_fiscal_paired_symbols':sum(not x['prior_315'] and x['paired_hours_in_FY']>0 for x in source_flags),
  'settlements':event_count,'zero_settlements':zero_count,'funding_events_using_hour_open_mark_proxy':proxy_marks,
  'historical_receive_timestamps_available':False,'historical_token_identity_verified_beyond_supplied_mapping':False,
  'historical_exact_margin_tiers_complete':False,'hashes':hashes,'per_symbol_coverage':source_flags,
  'limitations':['Legacy hourly price/volume retained in old 315-cohort; official warmup/tail and historical extension acquired.',
   'Depth remains a volume proxy with declared coefficients; scalar legacy spread/depth shape priors are not historical L2.',
   'Added archive pairs use supplied token mapping; archive existence is not proof of matching token identity across migrations.',
   'November interval uses previous observed settlement gap where archive interval is unavailable; first unknown interval is not backfilled from the future.']}
 A.js(ROOT/'data/MANIFEST.json',manifest);print(json.dumps({k:v for k,v in manifest.items() if k not in ['per_symbol_coverage','hashes','limitations']},indent=2),flush=True)
if __name__=='__main__':main()
