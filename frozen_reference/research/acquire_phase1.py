"""Audited public data acquisition for phase 1. No trading or private API calls."""
from pathlib import Path
import os
from datetime import date,timedelta
import concurrent.futures as cf,hashlib,io,json,threading,time,zipfile,xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import requests

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get('NFB_PHASE1_ROOT',str(HERE.parents[1]))).resolve()
PRIOR=Path(os.environ.get('NFB_PRIOR_ROOT',str(ROOT.parent/'neg-funding-v1-independent-replay-20261106'))).resolve()
BASE='https://data.binance.vision/data'
START=pd.Timestamp('2025-07-01',tz='UTC');END=pd.Timestamp('2026-11-06',tz='UTC')
T=int((END-START).total_seconds()/3600)
SOURCE=Path(os.environ.get('NFB_INVENTORY_TSV','/Users/leslie/.codex/attachments/d55a0c79-eaeb-465f-826a-cdcbffa90f74/Pasted text.txt'))
LOCAL=threading.local()
def js(path,x):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.writing');tmp.write_text(json.dumps(x,indent=2,allow_nan=False)+'\n');tmp.replace(path)
def sess():
 if not hasattr(LOCAL,'s'):LOCAL.s=requests.Session();LOCAL.s.headers['User-Agent']='negative-funding-phase1-audit/1.0'
 return LOCAL.s
def fetch(job):
 family,symbol,tag,url=job;dest=ROOT/'raw'/family/symbol/(tag+'.zip');recfile=dest.with_suffix('.json')
 if recfile.exists():
  old=json.loads(recfile.read_text())
  if old['status']=='unavailable_404':return old
  if old['status']=='ok' and dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()==old['sha256']:return old
 rec={'family':family,'symbol':symbol,'tag':tag,'url':url}
 for attempt in range(3):
  try:
   r=sess().get(url,timeout=(8,30));rec['http_status']=r.status_code
   if r.status_code==404:rec['status']='unavailable_404';break
   r.raise_for_status();c=sess().get(url+'.CHECKSUM',timeout=(8,20));c.raise_for_status()
   digest=hashlib.sha256(r.content).hexdigest()
   if c.text.split()[0]!=digest:raise ValueError('official checksum mismatch')
   with zipfile.ZipFile(io.BytesIO(r.content)) as z:
    if len(z.namelist())!=1:raise ValueError('unexpected members')
   dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(r.content)
   rec.update(status='ok',sha256=digest,bytes=len(r.content),official_checksum_verified=True,
    fetched_at_utc=pd.Timestamp.now(tz='UTC').isoformat());break
  except Exception as e:
   rec.update(status='error',error=type(e).__name__+': '+str(e));time.sleep(.3*(attempt+1))
 js(recfile,rec);return rec
def batch(jobs,label,workers=32):
 counts={};done=0;total=len(jobs);begin=time.monotonic()
 with cf.ThreadPoolExecutor(workers) as pool:
  futures={pool.submit(fetch,j):j for j in jobs}
  for f in cf.as_completed(futures):
   r=f.result();counts[r['status']]=counts.get(r['status'],0)+1;done+=1
   if done%200==0 or done==total:
    p={'stage':label,'done':done,'total':total,'counts':counts,'elapsed_seconds':round(time.monotonic()-begin,1)}
    js(ROOT/'acquisition_progress.json',p);print(json.dumps(p),flush=True)
 if counts.get('error',0):raise RuntimeError(f'{label}: explicit acquisition errors: {counts}')
 return counts
def archive_symbols(prefix):
 out=set();marker=None;pages=[]
 while True:
  params={'delimiter':'/','prefix':prefix,'max-keys':1000}
  if marker:params['marker']=marker
  r=sess().get('https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/',params=params,timeout=(8,25));r.raise_for_status()
  x=ET.fromstring(r.content);ns={'s':'http://s3.amazonaws.com/doc/2006-03-01/'}
  keys=[e.find('s:Prefix',ns).text for e in x.findall('s:CommonPrefixes',ns)]
  out.update(s[len(prefix):].strip('/') for s in keys);pages.append(hashlib.sha256(r.content).hexdigest())
  if x.findtext('s:IsTruncated',namespaces=ns)!='true':break
  marker=x.findtext('s:NextMarker',namespaces=ns) or keys[-1]
 return out,pages
def universe():
 dst=ROOT/'universe.json'
 if dst.exists():return json.loads(dst.read_text())
 meta=pd.read_csv(PRIOR/'data/ds/dataset_meta.csv');old={r.fut:r._asdict() for r in meta.itertuples(index=False)}
 spot,sp=archive_symbols('data/spot/monthly/klines/');fut,fp=archive_symbols('data/futures/um/monthly/klines/')
 raw=pd.read_csv(SOURCE,sep='\t',dtype=str).fillna('');records=[];excluded=[]
 for r in raw.to_dict('records'):
  if r['Binance Has Contract'].upper()!='Y':continue
  f=r['Binance Symbols'];s=old[f]['spot'] if f in old else r['Token'].upper()+'USDT'
  q=float(r['Borrowable Qty @ 30% (Token)'].replace(',',''));reason=None
  if q<=0:reason='zero_inventory'
  elif f not in fut:reason='no_perpetual_archive'
  elif s not in spot:reason='no_USDT_spot_archive_for_supplied_token_mapping'
  if reason:excluded.append({'perp_symbol':f,'reason':reason});continue
  rec={'fut':f,'spot':s,'mult':float(r['BN Multiplier (Token/Base)']),'prior_315':f in old}
  records.append(rec)
 out={'source':'supplied static inventory intersection with full historical archive directories, not current trading filter',
  'pair_candidates':records,'exclusions':excluded,'archive_symbol_counts':{'spot':len(spot),'perp':len(fut)},
  'listing_page_sha256':{'spot':sp,'perp':fp},'verified_token_identity_beyond_supplied_mapping':False}
 js(dst,out);print('Archive-paired candidates:',len(records),'exclusions:',len(excluded),flush=True);return out
def read_zip(path,family):
 with zipfile.ZipFile(path) as z:
  raw=z.read(z.namelist()[0])
 if family in ['spot','perp','mark']:
  header=raw.split(b'\n',1)[0].lower().startswith(b'open_time')
  f=pd.read_csv(io.BytesIO(raw),header=0 if header else None)
  if not header:f.columns=['open_time','open','high','low','close','volume','close_time','quote_volume','count','tb','tq','ignore']
  times=pd.to_numeric(f.open_time).to_numpy(np.int64);times=np.where(times>=10**14,times//1000,times)
  return f,times
 f=pd.read_csv(io.BytesIO(raw))
 return f,None
def price_jobs(u):
 jobs=[]
 for r in u['pair_candidates']:
  for month in pd.date_range('2025-07-01','2026-10-01',freq='MS'):
   if r['prior_315'] and month>=pd.Timestamp('2025-10-01'):continue
   tag=month.strftime('%Y-%m')
   for fam,sym,market in [('spot',r['spot'],'spot'),('perp',r['fut'],'futures/um'),('mark',r['fut'],'futures/um')]:
    kind='markPriceKlines' if fam=='mark' else 'klines'
    jobs.append((fam,sym,tag,f'{BASE}/{market}/monthly/{kind}/{sym}/1h/{sym}-1h-{tag}.zip'))
  for day in pd.date_range('2026-11-01','2026-11-05'):
   if r['prior_315'] and day<pd.Timestamp('2026-11-02'):continue
   tag=day.strftime('%Y-%m-%d')
   for fam,sym,market in [('spot',r['spot'],'spot'),('perp',r['fut'],'futures/um'),('mark',r['fut'],'futures/um')]:
    kind='markPriceKlines' if fam=='mark' else 'klines'
    jobs.append((fam,sym,tag,f'{BASE}/{market}/daily/{kind}/{sym}/1h/{sym}-1h-{tag}.zip'))
  for month in pd.date_range('2025-07-01','2026-10-01',freq='MS'):
   tag=month.strftime('%Y-%m');sym=r['fut']
   jobs.append(('funding',sym,tag,f'{BASE}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{tag}.zip'))
 return jobs
def market_arrays(u):
 symbols=[r['fut'] for r in u['pair_candidates']];N=len(symbols)
 arrays={k:np.full((T,N),np.nan) for k in ['sv','sc','sq','so','sh','sl','fv','fc','fq','fo','fh','fl','mk','mark_close','mark_low','mark_high','oih','oi']}
 oldmeta=pd.read_csv(PRIOR/'data/ds/dataset_meta.csv');offset=int((pd.Timestamp('2025-10-01',tz='UTC')-START).total_seconds()/3600)
 lookup={s:i for i,s in enumerate(oldmeta.fut)}
 pairs=[(n,lookup[r['fut']]) for n,r in enumerate(u['pair_candidates']) if r['fut'] in lookup]
 dest_cols,source_cols=map(list,zip(*pairs))
 with np.load(PRIOR/'data/ds/dataset.npz') as old:
  for k in arrays:
   if k in old.files:
    source=old[k]
    arrays[k][offset:offset+len(source),dest_cols]=source[:,source_cols]
 for n,r in enumerate(u['pair_candidates']):
  for fam,sym,pre in [('spot',r['spot'],'s'),('perp',r['fut'],'f'),('mark',r['fut'],'m')]:
   for path in sorted((ROOT/'raw'/fam/sym).glob('*.zip')):
    f,times=read_zip(path,fam);idx=((times-START.value//10**6)//3600000).astype(int);valid=(idx>=0)&(idx<T);idx=idx[valid];f=f.loc[valid]
    if len(np.unique(idx))!=len(idx):raise ValueError('duplicate hourly rows '+str(path))
    div=r['mult'] if fam!='spot' else 1.
    if fam=='mark':
     arrays['mk'][idx,n]=pd.to_numeric(f.open).to_numpy()/div
     for name,col in [('mark_close','close'),('mark_low','low'),('mark_high','high')]:arrays[name][idx,n]=pd.to_numeric(f[col]).to_numpy()/div
     continue
    volume=pd.to_numeric(f.volume).to_numpy(float);quote=pd.to_numeric(f.quote_volume).to_numpy(float)
    values=np.divide(quote,volume,out=np.zeros_like(quote),where=volume>0)/div
    for suffix,col in [('o','open'),('h','high'),('l','low'),('c','close')]:arrays[pre+suffix][idx,n]=pd.to_numeric(f[col]).to_numpy()/div
    arrays[pre+'v'][idx,n]=np.where(volume>0,values,0.);arrays[pre+'q'][idx,n]=quote
 return arrays
def oi_jobs(u,a):
 jobs=[]
 for n,r in enumerate(u['pair_candidates']):
  active=(a['sc'][:,n]>0)&(a['fc'][:,n]>0);need=active&~np.isfinite(a['oih'][:,n])
  days=sorted(set(np.flatnonzero(need)//24))
  # Previous day too, so prior-hour snapshots are available on the first active day.
  days=sorted(set(days)|{d-1 for d in days if d>0})
  for day in days:
   tag=(START+pd.Timedelta(days=day)).strftime('%Y-%m-%d');s=r['fut']
   jobs.append(('oi',s,tag,f'{BASE}/futures/um/daily/metrics/{s}/{s}-metrics-{tag}.zip'))
 return jobs
def main():
 u=universe();summary={};summary['prices_and_funding']=batch(price_jobs(u),'prices_and_funding')
 a=market_arrays(u);jobs=oi_jobs(u,a);summary['oi']=batch(jobs,'OI_warmup_and_tail')
 js(ROOT/'acquisition_summary.json',{'status':'complete','stages':summary,'candidate_symbols':len(u['pair_candidates']),
  'data_start_utc':str(START),'data_end_utc_exclusive':str(END),'note':'price/funding archives retained with official checksums; November funding requires public API stage'})
 print('PASS: public archive acquisition completed',flush=True)
if __name__=='__main__':main()
