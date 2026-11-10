"""Fixed-path accounting decomposition of frozen RC2; no fitting or execution."""
from pathlib import Path
import hashlib,json,os,sys,traceback
import numpy as np,pandas as pd
import argparse
REPO=Path(__file__).resolve().parents[1]
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--data',required=True,type=Path,help='Hash-matched market dataset ending2026-11-08')
ap.add_argument('--inventory',required=True,type=Path,help='Authorized private inventory CSV; not shipped')
ap.add_argument('--output',required=True,type=Path,help='New local output directory; contains private trade quantities')
ap.add_argument('--case',action='append',choices=['late_continuous_open','late_cold_open','early_continuous_open','early_cold_open','early_cold_unwind','early_continuous_unwind'],help='Repeat to select cases; default all six')
a=ap.parse_args();OUT=a.output.resolve();DATA=a.data.resolve();CAPS=a.inventory.resolve()
OUT.mkdir(parents=True,exist_ok=True)
SOURCE=REPO/'mock/reference/month_replay_original.py'
sys.path.insert(0,str(Path(__file__).resolve().parent));from pair_price_observer import build

def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for v in iter(lambda:f.read(4*1024*1024),b''):h.update(v)
 return h.hexdigest()
def save(name,x):
 p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def load_context():
 src=SOURCE.read_text();a="ROOT=Path(__file__).resolve().parents[1];ONLINE=ROOT.parent/'neg-funding-strategy-online-20261108';FROZEN=ONLINE/'frozen_reference'";b="CAPS=ROOT.parent.parent/'neg-funding-strategy-v1/private/inventory_caps.csv';DATA=ROOT/'data'"
 assert src.count(a)==src.count(b)==1
 src=src.replace(a,f"ROOT=Path({str(OUT)!r});ONLINE=Path({str(REPO)!r});FROZEN=ONLINE/'frozen_reference'").replace(b,f"CAPS=Path({str(CAPS)!r});DATA=Path({str(DATA)!r})")
 os.environ['NUMBA_CACHE_DIR']=str(OUT/'numba-cache');sys.argv=['price-observer']
 ctx={'__file__':str(SOURCE),'__name__':'price_observer_context'};exec(compile(src,str(SOURCE)+':relocated','exec'),ctx);ctx['setup']();return ctx

def main():
 if (OUT/'RESULTS.json').exists():raise ValueError('Do not overwrite completed decomposition')
 for child in ['src','audit','private']:(OUT/child).mkdir(exist_ok=True)
 (OUT/'private').chmod(0o700)
 original=json.loads((REPO/'mock/evidence/return-reconciliation/original-marked-summary.json').read_text())
 assert sha(CAPS)==original['inventory_source_sha256'],'Inventory hash mismatch; do not fabricate missing quantities'
 for rel,wanted in original['data_hashes'].items():assert sha(DATA/rel)==wanted,'Market input hash mismatch: '+rel
 assert sha(REPO/'frozen_reference/research/engine_phase1.py')==original['replay_engine_sha256']
 protocol={'capital_usdt':10000000,'primary':'2026-10-09 to2026-11-08 continuous marked, same frozen RC213.191989%',
  'same_path_not_zero_cost_policy_counterfactual':True,'separate_latency_slippage_in_model':False,
  'execution_impact_and_funding_own_impact_are_separate':True,'all_raw_quantities_private':True,
  'source_sha256':{'original_engine':sha(REPO/'frozen_reference/research/engine_phase1.py'),'frozen_bundle':sha(REPO/'runtime/models/r7-model-bundle.json'),
   'dataset':sha(DATA/'ds/dataset.npz'),'inventory':sha(CAPS),'runner':sha(__file__),'observer_builder':sha(REPO/'mock/pair_price_observer.py')},
  'cases':a.case or ['late_continuous_open','late_cold_open','early_continuous_open','early_cold_open','early_cold_unwind','early_continuous_unwind'],
  'fields':'market basis reference / modeled half spread / modeled square-root slippage-impact; no invented independent residual slip',
  'selected_coins':['HUMAUSDT','ERAUSDT','BMTUSDT'],'thresholds_or_model_changed':False,'live_orders':False,'real_forward_rows':0}
 save('PROTOCOL.json',protocol)
 B=load_context();D=B['D'];V=B['V'];RR=B['RR'];RA=B['RA'];HB=B['HB'];XE=B['XE'];N=B['N'];T=B['T'];RS=B['RS']
 eng=build(REPO/'frozen_reference/research/engine_phase1.py',OUT)
 expected=json.loads((REPO/'mock/evidence/return-reconciliation/RESULTS.json').read_text());exby={r['case']:r for r in expected['cases']}
 params={k:float(v) for k,v in B['RESEARCH']['parameters'].items()};params.update(tail_hours=0,trade_end=T+24.)
 rows=[];valid=[];stats=[]
 cases=[('late_continuous_open','2026-10-09','2026-11-08',True,False),('late_cold_open','2026-10-09','2026-11-08',False,False),
  ('early_continuous_open','2026-09-28','2026-10-28',True,False),('early_cold_open','2026-09-28','2026-10-28',False,False),
  ('early_cold_unwind','2026-09-28','2026-10-28',False,True),('early_continuous_unwind','2026-09-28','2026-10-28',True,True)]
 gate=RR.make_ok(('r7',2.),'cal');prior_sc=RA.shift(D.arr[1],1);prior_fc=RA.shift(D.arr[4],1)
 decision_spot=pd.DataFrame(D.arr[1]).where(lambda x:x>0).ffill().shift(1).to_numpy()
 decision_perp=pd.DataFrame(D.arr[4]).where(lambda x:x>0).ffill().shift(1).to_numpy()
 has=(prior_sc>0)&(prior_fc>0)&(D.arr[0]>0)&(D.arr[3]>0)
 deep=has&(RS.r8n<-.001);basisok=deep&(RA.b_prev<=-.003);qualified=basisok&gate
 for name,start,end,warm,unwind in cases:
  if name not in protocol['cases']:continue
  lo=RA.HABS(start);cut=RA.HABS(end);stop=cut+216 if unwind else cut;t0=RA.A0 if warm else lo
  RR.WINS[name]=(t0,cut);ex=dict(params)
  if unwind:ex['tail_hours']=216
  HB.E=XE;HB.mkp=XE.mkp
  baseline=V.run_fix('cons_c0',name,extra=ex,oi_frac=.025,m=2.)
  times=np.array([lo-1,cut-1,stop-1],np.int64);snaps=np.zeros((3,N,16));trace=np.zeros((250000,18));ntrace=np.zeros(1,np.int64)
  def observed(*a,**kw):return eng.sim(*a,**kw,coin_snapshots=snaps,snapshot_hours=times,price_trace=trace,trace_count=ntrace)
  HB.E=type('Observed',(),{'PN':XE.PN,'sim':staticmethod(observed)})
  r=V.run_fix('cons_c0',name,extra=ex,oi_frac=.025,m=2.)
  errors={k:float(np.max(abs(r[k]-baseline[k]))) for k in ['cum','comp','pos','accounts','ep'] if baseline[k].size}
  assert max(errors.values())<1e-7,(name,errors);assert r['resolved_engine_parameters']==baseline['resolved_engine_parameters']
  i=lo-t0;previous=baseline['cum'][i-1] if i else 0;net=float(baseline['cum'][-1]-previous)
  assert abs(net-exby[name]['net_usdt'])<1e-6
  trace=trace[:int(ntrace[0])].copy();rawpath=OUT/'private'/f'{name}-trace.npz';np.savez_compressed(rawpath,trace=trace,snapshots=snaps,symbols=np.array(D.fut));rawpath.chmod(0o600)
  period=snaps[2]-snaps[0];pair=period[:,1]+period[:,3];spread=period[:,6];impact=period[:,7]
  ref=period[:,8]+period[:,9];netcoin=period[:,0]+pair-period[:,2]
  addbackerr=float(np.max(abs(ref-(pair+spread+impact))));costerr=float(np.max(abs(period[:,4]-spread-impact)))
  assert addbackerr<1e-6 and costerr<1e-6,(name,addbackerr,costerr)
  # Independent vectorized trade attribution, separate from counters in observer.
  select=trace[(trace[:,0]>=lo)&(trace[:,0]<stop)];ns=select[:,1].astype(int);q=select[:,3]
  sp=q*select[:,4]*select[:,8];fp=q*select[:,5]*select[:,9];si=q*select[:,4]*select[:,10];fi=q*select[:,5]*select[:,11]
  outside=(select[:,6]-select[:,4])*q;outside=np.abs(outside)+(np.abs(select[:,7]-select[:,5])*q)
  vectorerr=max(float(np.max(abs(np.bincount(ns,weights=sp+fp,minlength=N)-spread))),float(np.max(abs(np.bincount(ns,weights=si+fi,minlength=N)-impact))))
  assert vectorerr<1e-6 and abs(outside.sum()-(spread+impact).sum())<1e-6
  # Funding recomputation uses actual held-before-settlement quantities, settled
  # raw events and the separate frozen k4 adjustment; no strategy-state change.
  price=pd.DataFrame(D.arr[1]).where(lambda x:x>0).ffill().to_numpy()[t0:stop]
  qty=np.divide(baseline['pos'],price,out=np.zeros_like(baseline['pos']),where=price>0)
  prevq=np.r_[np.zeros((1,N)),qty[:-1]];fcprev=pd.DataFrame(D.arr[4]).where(lambda x:x>0).ffill().shift(1).to_numpy()[t0:stop]
  prevq=np.where(np.arange(t0,stop)[:,None]>=D.dl_fin[None,:],0.,prevq)
  mk=np.where(D.mk[t0:stop]>0,D.mk[t0:stop],fcprev);ev=D.arr[6][t0:stop];iv=D._iv[t0:stop];oiv=D.oiv_for('hour',1)[t0:stop]
  gross=-np.nan_to_num(ev)*np.nan_to_num(mk)*prevq
  bump=np.divide(.04*prevq*np.nan_to_num(fcprev),oiv,out=np.zeros_like(prevq),where=oiv>0)
  own=np.where((np.nan_to_num(ev)!=0)|(iv>0),bump*np.where(iv>0,iv,8)/8*np.nan_to_num(mk)*prevq,0.)
  ferr=float(np.max(abs((gross-own)[i:].sum(axis=0)-period[:,0])));oerr=float(np.max(abs(own[i:].sum(axis=0)-period[:,5])))
  assert ferr<1e-6 and oerr<1e-6
  # No raw inventory/quantity fields in per-coin report.
  case=[]
  for n,sym in enumerate(D.fut):
   active=bool(np.any(abs(period[n,:10])>1e-8))
   if not active:continue
   trade=select[ns==n];held=prevq[i:,n]>1e-12;positive=qty[i:,n]>1e-12
   row={'case':name,'symbol':sym,'market_basis_reference_pnl_usdt':float(ref[n]),'modeled_spread_cost_usdt':float(spread[n]),
    'modeled_sqrt_slippage_impact_cost_usdt':float(impact[n]),'pair_price_net_usdt':float(pair[n]),'funding_net_usdt':float(period[n,0]),
    'market_funding_before_k_usdt':float(period[n,0]+period[n,5]),'own_funding_k4_deduction_usdt':float(period[n,5]),
    'fees_usdt':float(period[n,2]),'net_marked_pnl_usdt':float(netcoin[n]),'held_before_settlement_hours':int(held.sum()),
    'ending_open':bool(snaps[2,n,10]>1e-12),'modeled_spread_spot_usdt':float(period[n,12]),'modeled_spread_perp_usdt':float(period[n,13]),
    'modeled_impact_spot_usdt':float(period[n,14]),'modeled_impact_perp_usdt':float(period[n,15]),
    'fill_count':int(len(trade)),'entry_fill_count':int((trade[:,2]>0).sum()),'exit_fill_count':int((trade[:,2]<0).sum())}
   case.append(row)
  frame=pd.DataFrame(case);frame.to_csv(OUT/f'{name}-coin-components.csv',index=False)
  total={'case':name,'start_utc':start,'entry_end_exclusive_utc':end,'report_hours':stop-lo,
   'market_basis_reference_pnl_usdt':float(ref.sum()),'modeled_spread_cost_usdt':float(spread.sum()),
   'modeled_sqrt_slippage_impact_cost_usdt':float(impact.sum()),'pair_price_net_usdt':float(pair.sum()),
   'funding_net_usdt':float(period[:,0].sum()),'market_funding_before_k_usdt':float((period[:,0]+period[:,5]).sum()),
   'own_funding_k4_deduction_usdt':float(period[:,5].sum()),'fees_usdt':float(period[:,2].sum()),'net_marked_pnl_usdt':float(netcoin.sum()),
   'annualized_marked_pct':float(netcoin.sum()/1e7*8760/(stop-lo)*100),'ending_open_symbols':int((snaps[2,:,10]>1e-12).sum()),
   'modeled_spread_share_of_execution_cost':float(spread.sum()/(spread+impact).sum()),'modeled_impact_share_of_execution_cost':float(impact.sum()/(spread+impact).sum()),
   'separate_latency_slippage':None,'fill_count':len(select),'spot_spread_cost_usdt':float(period[:,12].sum()),'perp_spread_cost_usdt':float(period[:,13].sum()),
   'spot_sqrt_impact_cost_usdt':float(period[:,14].sum()),'perp_sqrt_impact_cost_usdt':float(period[:,15].sum())}
  obs_tt=select[:,0].astype(int);obs_nn=select[:,1].astype(int)
  drift=float((select[:,2]*select[:,3]*((select[:,4]-select[:,5])-(decision_spot[obs_tt,obs_nn]-decision_perp[obs_tt,obs_nn]))).sum())
  total['decision_close_basis_reference_pnl_usdt']=total['market_basis_reference_pnl_usdt']-drift
  total['execution_hour_basis_drift_proxy_usdt']=drift
  total['execution_hour_proxy_is_not_observed_latency_slippage']=True
  assert abs(total['decision_close_basis_reference_pnl_usdt']+drift-total['modeled_spread_cost_usdt']-total['modeled_sqrt_slippage_impact_cost_usdt']-total['pair_price_net_usdt'])<1e-6
  assert abs(total['net_marked_pnl_usdt']-net)<1e-6
  rows.append(total);valid.append({'case':name,'original_path_max_errors':errors,'per_coin_identity_error':addbackerr,'spread_plus_impact_error':costerr,
   'independent_vector_trade_error':vectorerr,'independent_funding_error':ferr,'independent_own_funding_error':oerr,'original_endpoint_error':abs(net-exby[name]['net_usdt']),
   'raw_trace_sha256':sha(rawpath),'raw_trade_count':len(trace),'period_trade_count':len(select)})
  print(json.dumps(total),flush=True)
  if name in ['late_continuous_open','early_continuous_open']:
   for sym in protocol['selected_coins']:
    n=D.fut.index(sym);hh=prevq[i:,n]>1e-12;positive=qty[i:,n]>1e-12;absheld=np.flatnonzero(hh)+lo
    # Quote notional capacity is sensitive; keep these supplementary diagnostics private.
    private={'case':name,'symbol':sym,'mean_calendar_notional_usdt':float(baseline['pos'][i:,n].mean()),
     'mean_held_notional_usdt':float(baseline['pos'][i:,n][positive].mean()) if positive.any() else 0.}
    save('private/'+name+'-'+sym+'-position.json',private)
    events=np.flatnonzero(D._iv[lo:cut,n]>0)+lo;rates=D.arr[6][events,n]
    first_last=[str(B['START']+pd.Timedelta(hours=int(x))) for x in [absheld[0],absheld[-1]]] if len(absheld) else []
    stats.append({'case':name,'symbol':sym,'observed_settlements':int(len(events)),'negative_settlements':int((rates<0).sum()),'nonnegative_settlements':int((rates>=0).sum()),
     'deep_r8_qualified_hours':int(deep[lo:cut,n].sum()),'rate_and_basis_hours':int(basisok[lo:cut,n].sum()),'rate_basis_income_adl_hours':int(qualified[lo:cut,n].sum()),
     'held_before_settlement_hours':int(hh.sum()),'first_and_last_held_hour_utc':first_last,'raw_negative_settled_rate_sum':float(rates[rates<0].sum())})
  del r,baseline,trace,snaps,price,qty,prevq,gross,own
 save('RESULTS.json',{'protocol':protocol,'cases':rows,'selected_coin_signal_statistics':stats,'validation':valid,
  'model_cost_decomposition_not_actual_fill_calibration':True,'all_checks_passed':True,'submission_allowed':False})
 pd.DataFrame(rows).to_csv(OUT/'SUMMARY.csv',index=False);pd.DataFrame(stats).to_csv(OUT/'SELECTED_COINS_SIGNALS.csv',index=False)
 save('VALIDATION.json',{'passed':True,'controlled_replays':len(rows),'telemetry_preserves_hourly_comp_position_accounts_episodes':True,
  'independent_trade_vector_checks':True,'independent_basis_reference_identity':True,'independent_funding_reconstruction':True,'raw_quantities_private':True,'frozen_source_unchanged':sha(REPO/'frozen_reference/research/engine_phase1.py')==protocol['source_sha256']['original_engine']})
if __name__=='__main__':
 try:main()
 except Exception:
  save('FAILURE.json',{'traceback':traceback.format_exc(),'original_sources_modified':False});raise
