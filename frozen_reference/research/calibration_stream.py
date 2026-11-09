"""Rebuild v1 calibration from official archives; no simulated observations.

AggTrades: original 28-day calendar/formulas, bounded-memory aggregation.
BookDepth: reuse validated local raw-derived daily snapshots; median of usable positive bands.
The original historical input hashes remain unknown, so this is an independent replay.
"""
from pathlib import Path
from datetime import date,timedelta
import concurrent.futures as cf
import csv,hashlib,io,json,os,threading,time,tempfile,zipfile
import numpy as np
import pandas as pd
import requests

import acquire_phase1 as A
ROOT=A.ROOT
BASE='https://data.binance.vision/data/futures/um/daily'
LOCAL=threading.local();CPU=threading.Semaphore(4)
(ROOT/'calib').mkdir(exist_ok=True);(ROOT/'temp').mkdir(exist_ok=True);(ROOT/'records').mkdir(exist_ok=True)

def sha(path):
 with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def atomic_json(path,obj):
 path=Path(path);tmp=path.with_suffix(path.suffix+'.writing');tmp.write_text(json.dumps(obj,indent=2,allow_nan=False)+'\n');tmp.replace(path)

def safe(value):
 if isinstance(value,dict):return {k:safe(v) for k,v in value.items()}
 if isinstance(value,(list,tuple)):return [safe(v) for v in value]
 if isinstance(value,(float,np.floating)):return float(value) if np.isfinite(value) else None
 if isinstance(value,np.integer):return int(value)
 return value

def session():
 if not hasattr(LOCAL,'s'):
  LOCAL.s=requests.Session();LOCAL.s.headers['User-Agent']='v1-calibration-independent-replay/1.0'
 return LOCAL.s

def aggregate(path,day):
 """Same y_kyle/hs formulas as do_ag; unused raw-price median is not emitted."""
 left=int(pd.Timestamp(day,tz='UTC').timestamp()*1000)
 seconds=np.zeros((86400,4));Q=np.zeros(288);V=np.zeros(288)
 p0=np.full(288,np.nan);p1=np.full(288,np.nan);count=0;last_id=None;last_time=None
 with zipfile.ZipFile(path) as z:
  member=z.namelist()[0]
  chunks=pd.read_csv(z.open(member),usecols=lambda c:c in ('agg_trade_id','price','quantity','transact_time','is_buyer_maker'),chunksize=200000)
  for frame in chunks:
   needed={'price','quantity','transact_time','is_buyer_maker'}
   if not needed.issubset(frame):raise ValueError('missing aggTrade columns')
   t=frame.transact_time.to_numpy(np.int64);p=frame.price.to_numpy(float);qty=frame.quantity.to_numpy(float)
   if len(t)==0:continue
   if not ((t>=left)&(t<left+86400000)).all():raise ValueError('timestamps outside day / unexpected units')
   if np.any(np.diff(t)<0) or (last_time is not None and t[0]<last_time):raise ValueError('out-of-order trade time')
   last_time=int(t[-1])
   if 'agg_trade_id' in frame:
    ids=frame.agg_trade_id.to_numpy(np.int64)
    if np.any(np.diff(ids)<=0) or (last_id is not None and ids[0]<=last_id):raise ValueError('duplicate/out-of-order agg IDs')
    last_id=int(ids[-1])
   if not np.isfinite(p).all() or not np.isfinite(qty).all() or (p<=0).any() or (qty<=0).any():raise ValueError('invalid prices or quantities')
   maker=frame.is_buyer_maker
   if maker.dtype==object:
    text=maker.astype(str).str.lower()
    if not text.isin(['true','false']).all():raise ValueError('invalid aggressor flags')
    maker=(text=='true')
   buy=~maker.to_numpy(bool);notional=p*qty;sec=((t-left)//1000).astype(int);bk=((t-left)//300000).astype(int)
   for c,w in enumerate([np.where(buy,notional,0.),np.where(buy,0.,notional),np.where(buy,p*notional,0.),np.where(buy,0.,p*notional)]):
    seconds[:,c]+=np.bincount(sec,weights=w,minlength=86400)
   Q+=np.bincount(bk,weights=np.where(buy,notional,-notional),minlength=288)
   V+=np.bincount(bk,weights=notional,minlength=288)
   keys,first=np.unique(bk,return_index=True);unset=np.isnan(p0[keys]);p0[keys[unset]]=p[first[unset]]
   reverse_keys,reverse_first=np.unique(bk[::-1],return_index=True);p1[reverse_keys]=p[len(p)-1-reverse_first]
   count+=len(frame)
 if not count:raise ValueError('empty archive')
 active=np.flatnonzero(np.isfinite(p1));lo,hi=active[0],active[-1]+1
 start,end=p0[lo:hi],p1[lo:hi];flow=Q[lo:hi];vol=V[lo:hi]
 r5=np.nan_to_num(np.log(end/start),nan=0.)
 returns=np.log(end[1:]/end[:-1]);returns=returns[np.isfinite(returns)]
 sig=float(np.std(returns)*np.sqrt(288.))
 adv=float(vol.sum());x=sig*np.sign(flow)*np.sqrt(np.abs(flow)/max(adv,1.));m=np.isfinite(x)&(x!=0)
 y=float((x[m]*r5[m]).sum()/(x[m]**2).sum()) if m.sum()>30 else np.nan
 both=(seconds[:,0]>0)&(seconds[:,1]>0);bn,sn,bp,sp=seconds[both].T
 hs=float(np.median((bp/bn-sp/sn)/(bp/bn+sp/sn))) if both.sum()>50 else np.nan
 return {'hs':hs,'y_kyle':y,'sig_day':sig,'adv':adv,'n_trd':count,'n_sec':int(both.sum())}

def fetch_reduce(job):
 sym,day=job;record=ROOT/'records'/f'{sym}_{day}.json'
 if record.exists():
  old=json.loads(record.read_text())
  if old['status'] in ('ok','not_listed','unavailable_404'):
   if old['status']=='not_listed':old['status']='unavailable_404';old['reason']='HTTP 404, not proof of listing history';atomic_json(record,old)
   return old
 url=f'{BASE}/aggTrades/{sym}/{sym}-aggTrades-{day}.zip'
 rec={'sym':sym,'date':day,'url':url};temp=None
 for attempt in range(3):
  try:
   with session().get(url,stream=True,timeout=(10,45)) as r:
    if r.status_code==404:
     rec.update(status='unavailable_404',http_status=404,reason='HTTP 404, not proof of listing history');atomic_json(record,rec);return rec
    r.raise_for_status()
    fd,name=tempfile.mkstemp(prefix='ag-',suffix='.zip',dir=ROOT/'temp');os.close(fd);temp=Path(name)
    h=hashlib.sha256();size=0
    with temp.open('wb') as f:
     for chunk in r.iter_content(1024*1024):
      if chunk:f.write(chunk);h.update(chunk);size+=len(chunk)
    rec.update(zip_sha256=h.hexdigest(),zip_bytes=size)
   check=session().get(url+'.CHECKSUM',timeout=(10,20))
   if check.status_code!=200:raise ValueError('official checksum unavailable: '+str(check.status_code))
   expected=check.text.split()[0]
   if expected!=rec['zip_sha256']:raise ValueError('official checksum mismatch')
   rec['official_checksum_verified']=True
   with CPU:values=aggregate(temp,day)
   rec.update(status='ok',values=safe(values));atomic_json(record,rec)
   temp.unlink();return rec
  except (requests.RequestException,ValueError,OSError,zipfile.BadZipFile,pd.errors.ParserError) as e:
   if temp is not None and temp.exists():temp.unlink()
   rec.update(status='error',error=type(e).__name__+': '+str(e),attempt=attempt+1)
   if attempt<2:time.sleep(.3*(attempt+1))
 atomic_json(record,rec);return rec
