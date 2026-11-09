"""Outcome-independent stratified sample of modeled fill days; not L2 proof."""
from pathlib import Path
import os
import concurrent.futures as cf,hashlib,io,json,zipfile
import numpy as np
import pandas as pd
import acquire_phase1 as A

ROOT=A.ROOT
OUT=Path(os.environ.get('NFB_MINUTE_OUTPUT',str(ROOT)));OUT.mkdir(parents=True,exist_ok=True)
def frame(family,symbol,day):
 rec=A.fetch((family,symbol,day,f'{A.BASE}/{"spot" if family=="spot1m" else "futures/um"}/daily/klines/{symbol}/1m/{symbol}-1m-{day}.zip'))
 if rec['status']!='ok':return rec,None
 path=ROOT/'raw'/family/symbol/(day+'.zip')
 with zipfile.ZipFile(path) as z:b=z.read(z.namelist()[0])
 header=b.split(b'\n',1)[0].lower().startswith(b'open_time')
 f=pd.read_csv(io.BytesIO(b),header=0 if header else None)
 if not header:f.columns=['open_time','open','high','low','close','volume','close_time','quote_volume','count','tb','tq','ignore']
 ms=pd.to_numeric(f.open_time).to_numpy(np.int64);ms=np.where(ms>=10**14,ms//1000,ms)
 f.index=ms
 return rec,f
def main():
 meta=pd.read_csv(ROOT/'data/ds/dataset_meta.csv')
 position_file=Path(os.environ.get('NFB_POSITION_AUDIT',ROOT/'private/reference_positions.npz'))
 position_sha=hashlib.sha256(position_file.read_bytes()).hexdigest()
 with np.load(position_file) as z:
  qty=z['qty_token'];start=int(z['start_hour']);symbols=z['symbols']
 assert np.array_equal(meta.fut.to_numpy(),symbols)
 delta=np.diff(np.vstack([np.zeros((1,len(meta))),qty]),axis=0)
 jobs={}
 for t,n in zip(*np.where(np.abs(delta)>1e-8)):
  day=(A.START+pd.Timedelta(hours=start+int(t))).strftime('%Y-%m-%d')
  jobs.setdefault((int(n),day),[]).append(int(t))
 bymonth={}
 for n,day in jobs:bymonth.setdefault(day[:7],[]).append((n,day))
 chosen=[]
 for month,items in sorted(bymonth.items()):
  chosen+=sorted(items,key=lambda x:hashlib.sha256((str(symbols[x[0]])+x[1]).encode()).hexdigest())[:4]
 output=[]
 def one(job):
  n,day=job;r=meta.iloc[n];sp,s=frame('spot1m',r.spot,day);fp,f=frame('perp1m',r.fut,day)
  if s is None or f is None:return [{'day':day,'symbol':r.fut,'status':'archive_missing','spot_status':sp['status'],'perp_status':fp['status']}]
  pairs=s.join(f,how='inner',lsuffix='_s',rsuffix='_f')
  out=[]
  for t in jobs[job]:
   left=int((A.START+pd.Timedelta(hours=start+t)).timestamp()*1000)
   hour=pairs[(pairs.index>=left)&(pairs.index<left+3600000)]
   if hour.empty:continue
   vs=hour.volume_s.to_numpy(float);vf=hour.volume_f.to_numpy(float)*r.mult
   ps=np.divide(hour.quote_volume_s.to_numpy(float),vs,out=np.zeros_like(vs),where=vs>0)
   pf=np.divide(hour.quote_volume_f.to_numpy(float),vf,out=np.zeros_like(vf),where=vf>0)
   good=(vs>0)&(vf>0)&(ps>0)&(pf>0)
   capacity=.03*np.minimum(vs,vf);want=abs(delta[t,n]);sign=1 if delta[t,n]>0 else -1
   old_s=hour.quote_volume_s.sum()/max(hour.volume_s.sum(),1e-20)
   old_f=hour.quote_volume_f.sum()/max(hour.volume_f.sum()*r.mult,1e-20)
   for delay in [1,5]:
    rem=want;filled=0.;basis_cost=0.
    for i,stamp in enumerate(hour.index):
     if stamp<left+delay*60000 or not good[i]:continue
     take=min(rem,capacity[i]);filled+=take;rem-=take
     basis_cost+=sign*take*((pf[i]-ps[i])-(old_f-old_s))
     if rem<=1e-10:break
    out.append({'day':day,'symbol':r.fut,'status':'observed_minute_bars',
     'delay_minutes':delay,'minute_rows_in_hour':len(hour),
     'modeled_net_change_usdt':float(want*old_s),'matched_fraction_under_3pct_minute_cap':float(filled/want),
     'paired_price_difference_usdt_vs_whole_hour_VWAP':float(basis_cost),
     'method':'Net hourly position change lower bound; 3pct of min spot/perp minute raw-token volume; no spread, L2, queue or leg-delay proof.'})
  return out
 with cf.ThreadPoolExecutor(10) as pool:
  for result in pool.map(one,chosen):output.extend(result)
 pd.DataFrame(output).to_csv(OUT/'minute_sample_audit.csv',index=False)
 summaries={}
 for delay in [1,5]:
  rows=[r for r in output if r.get('delay_minutes')==delay]
  summaries[str(delay)+'min']={'hourly_net_changes':len(rows),
   'fully_matched_hours':sum(r['matched_fraction_under_3pct_minute_cap']>=.999999 for r in rows),
   'total_modeled_net_change_usdt':sum(r['modeled_net_change_usdt'] for r in rows),
   'paired_price_difference_usdt':sum(r['paired_price_difference_usdt_vs_whole_hour_VWAP'] for r in rows),
   'notional_weighted_fill_fraction':sum(r['modeled_net_change_usdt']*r['matched_fraction_under_3pct_minute_cap'] for r in rows)/sum(r['modeled_net_change_usdt'] for r in rows) if rows else None}
 A.js(OUT/'minute_sample_summary.json',{'sample_days':len(chosen),'sampling':'up to four symbol-days per month, SHA256 order; independent of profit and loss',
  'archive_missing_days':sum(r['status']=='archive_missing' for r in output),'results':summaries,
  'position_input_sha256':position_sha,'actual_execution_verified':False,'cannot_extrapolate_sample_price_difference_to_annual_PnL':True})
 print(json.dumps(summaries,indent=2),flush=True)
if __name__=='__main__':main()
