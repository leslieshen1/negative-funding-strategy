"""Public input hashes, archive integrity and mapping checks. Never exports caps."""
from pathlib import Path
import concurrent.futures as cf,hashlib,json,io
import numpy as np
import pandas as pd
import requests
from acquire_phase1 import ROOT,START,js
from units import paired_token_step

def main():
 manifest=json.loads((ROOT/'data/MANIFEST.json').read_text())
 for name,digest in manifest['hashes'].items():
  with (ROOT/'data'/name).open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==digest
 records=list((ROOT/'raw').rglob('*.json'));counts={};verified=0;missing=0;errors=[]
 for p in records:
  j=json.loads(p.read_text());status=j.get('status');counts[status]=counts.get(status,0)+1
  if status=='ok' and j.get('official_checksum_verified'):
   z=p.with_suffix('.zip')
   if not z.exists():missing+=1;continue
   with z.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
   if digest!=j['sha256']:errors.append(str(p))
   else:verified+=1
 assert not errors and not missing
 meta=pd.read_csv(ROOT/'data/ds/dataset_meta.csv')
 with np.load(ROOT/'data/ds/dataset.npz') as z:
  sc,fc,sq,fq=[z[k] for k in ['sc','fc','sq','fq']];iv=z['ivh'];ev=z['ev']
  assert ev.shape==iv.shape and np.isfinite(ev).all()
  rows=[]
  for n,r in enumerate(meta.itertuples(index=False)):
   good=(sc[:,n]>0)&(fc[:,n]>0)&(sq[:,n]>0)&(fq[:,n]>0)
   ratio=fc[good,n]/sc[good,n]
   rows.append({'perp_symbol':r.fut,'spot_symbol':r.spot,'tokens_per_perp_quantity':r.mult,'paired_hours':int(good.sum()),
    'first_100_paired_hours_price_ratio_median':float(np.median(ratio[:100])) if len(ratio) else None,
    'full_sample_price_ratio_median':float(np.median(ratio)) if len(ratio) else None})
  pd.DataFrame(rows).to_csv(ROOT/'instrument_mapping_audit.csv',index=False)
 urls={'spot':'https://api.binance.com/api/v3/exchangeInfo','perp':'https://fapi.binance.com/fapi/v1/exchangeInfo'}
 specs={}
 for kind,url in urls.items():
  try:
   r=requests.get(url,timeout=(8,25));r.raise_for_status();d=r.json()
   js(ROOT/'raw/current_specs'/f'{kind}.json',{'url':url,'fetched_at_utc':pd.Timestamp.now(tz='UTC').isoformat(),
    'sha256':hashlib.sha256(r.content).hexdigest(),'historical':False,'data':d})
   specs[kind]={x['symbol']:x for x in d['symbols']}
  except Exception as e:specs[kind]={};js(ROOT/'raw/current_specs'/f'{kind}.json',{'url':url,'status':'unavailable','error':str(e)})
 steps=[]
 for row in rows:
  a=specs['spot'].get(row['spot_symbol']);b=specs['perp'].get(row['perp_symbol'])
  if not a or not b:continue
  af={x['filterType']:x for x in a['filters']};bf={x['filterType']:x for x in b['filters']}
  sa=af.get('LOT_SIZE',{}).get('stepSize');sb=bf.get('LOT_SIZE',{}).get('stepSize')
  if not sa or not sb:continue
  steps.append({'perp_symbol':row['perp_symbol'],'spot_symbol':row['spot_symbol'],
   'spot_step_token':sa,'perp_step_exchange':sb,'tokens_per_perp_quantity':str(row['tokens_per_perp_quantity']),
   'paired_raw_token_quantum':format(paired_token_step(sa,sb,row['tokens_per_perp_quantity']),'f'),
   'current_snapshot_only':True,'market_order_specific_filters_still_required':True})
 pd.DataFrame(steps).to_csv(ROOT/'current_quantity_steps.csv',index=False)
 js(ROOT/'INPUT_VALIDATION.json',{'status':'passed','input_hashes_verified':len(manifest['hashes']),
  'official_archive_records_verified':verified,'archive_errors':errors,'missing_archive_files':missing,
  'response_status_counts':counts,'mapped_instruments':len(rows),'current_quantity_specs_available':len(steps),
  'historical_quantity_steps_verified':False,'continuous_quantity_backtest':True,
  'price_ratio_is_sanity_check_not_token_identity_proof':True,'private_inventory_quantities_exported':False})
 print('PASS: input/archive hashes and mappings',verified,'archives',len(steps),'current quantity specs',flush=True)
if __name__=='__main__':main()
