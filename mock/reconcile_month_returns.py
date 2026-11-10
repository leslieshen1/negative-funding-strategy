"""Compare original RC2 month reports through the same frozen public engine.
No market downloads, model artifact writes, parameter search or ledger mutation.
"""
from pathlib import Path
import json,hashlib,os,sys,time,traceback
import numpy as np
import pandas as pd
import argparse
REPO=Path(__file__).resolve().parents[1]
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--data',required=True,type=Path,help='Hash-matched extended public market dataset root, ending2026-11-08')
ap.add_argument('--cohort-data',required=True,type=Path,help='Original cohort market data root, ending2026-11-06')
ap.add_argument('--inventory',required=True,type=Path,help='Authorized private inventory CSV; never exported')
ap.add_argument('--output',required=True,type=Path,help='A new output directory')
a=ap.parse_args()
OUT=a.output.resolve();OUT.mkdir(parents=True,exist_ok=True)
DATA=a.data.resolve();COHORT_DATA=a.cohort_data.resolve();CAPS=a.inventory.resolve()
ONLINE=REPO;SOURCE=REPO/'mock/reference/month_replay_original.py'
EXPECTED_MONTH=REPO/'mock/evidence/return-reconciliation/original-marked-summary.json'
EXPECTED_COHORT=REPO/'mock/evidence/historical-regression.json'
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
 return h.hexdigest()
def save(name,x):
 (OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def main():
 if (OUT/'RESULTS.json').exists():raise ValueError('Do not overwrite completed reconciliation')
 (OUT/'audit').mkdir(exist_ok=True);(OUT/'curves').mkdir(exist_ok=True)
 old=json.loads(EXPECTED_MONTH.read_text());prior=json.loads(EXPECTED_COHORT.read_text());target=next(x for x in prior['cases'] if x['window']=='M30')
 assert sha(CAPS)==old['inventory_source_sha256'],'Private inventory differs; do not invent matching quantities'
 assert sha(ONLINE/'frozen_reference/research/engine_phase1.py')==old['replay_engine_sha256']
 for rel,wanted in old['data_hashes'].items():assert sha(DATA/rel)==wanted,'Extended data hash mismatch: '+rel
 for rel,wanted in prior['protocol']['input_hashes'].items():assert sha(COHORT_DATA/rel)==wanted,'Cohort data hash mismatch: '+rel
 protocol={'purpose':'Explain 13.191989% vs 5.859245%; dates, initial state, finite-horizon unwind, elapsed-time denominator',
  'capital_usdt':10000000,'annualization':'simple, 8760h; marked vs completed labels kept separate',
  'window_early':['2026-09-28','2026-10-28'],'window_late':['2026-10-09','2026-11-08'],
  'common_data_root':str(DATA),'common_data_end_exclusive':'2026-11-08',
  'same_frozen_engine':True,'same_frozen_monthly_model_bundle':True,'strategy_or_model_artifacts_changed':False,
  'old_data_is_not_new_OOS':True,'real_orders':False,'live_ledger_writes':False,
  'source_hashes':{'adapter':sha(__file__),'original_month_runner':sha(SOURCE),'original_result':sha(EXPECTED_MONTH),
    'later_result':sha(EXPECTED_COHORT),'dataset':sha(DATA/'ds/dataset.npz'),'private_inventory':sha(CAPS),
    'frozen_model_bundle':sha(ONLINE/'runtime/models/r7-model-bundle.json')},
  'cases':['late_continuous_open','late_cold_open','early_continuous_open','early_cold_open','early_cold_unwind','early_continuous_unwind'],
  'unwind_hours':216,'unwind_starts_hours_before_entry_window_end':24,
  'attribution_order':['initial_state','date_window','last_24h_end_policy','subsequent_216h_tail_pnl','30_to_39_day_denominator'],
  'late_completed_return':'Not available: 9d followup after Nov8 extends to Nov17, beyond current data and client time'}
 save('PROTOCOL.json',protocol)
 # Relocate outputs/data references in an in-memory copy; preserve original source.
 src=SOURCE.read_text()
 a="ROOT=Path(__file__).resolve().parents[1];ONLINE=ROOT.parent/'neg-funding-strategy-online-20261108';FROZEN=ONLINE/'frozen_reference'"
 b="CAPS=ROOT.parent.parent/'neg-funding-strategy-v1/private/inventory_caps.csv';DATA=ROOT/'data'"
 assert src.count(a)==src.count(b)==1
 src=src.replace(a,f"ROOT=Path({str(OUT)!r});ONLINE=Path({str(ONLINE)!r});FROZEN=ONLINE/'frozen_reference'")
 src=src.replace(b,f"CAPS=Path({str(CAPS)!r});DATA=Path({str(DATA)!r})")
 os.environ['NUMBA_CACHE_DIR']=str(OUT/'numba-cache');sys.argv=['reconciliation']
 ctx={'__file__':str(SOURCE),'__name__':'reconciliation_original_context'}
 exec(compile(src,str(SOURCE)+':output_relocated','exec'),ctx)
 ctx['setup']();D=ctx['D'];V=ctx['V'];RR=ctx['RR'];RA=ctx['RA'];P=ctx['P'];RESEARCH=ctx['RESEARCH'];T=ctx['T'];N=ctx['N'];S=1e7
 # Source data equality is recorded in the original extended data manifest; verify
 # that its pinned old file is still the prior experiment's source, not just equal dates.
 manifest=json.loads((DATA/'MANIFEST.json').read_text());assert all(manifest['old_prefix_arrays_identical'].values())
 prefix_file=COHORT_DATA/'ds/dataset.npz'
 assert sha(prefix_file)==prior['protocol']['input_hashes']['ds/dataset.npz']
 params={k:float(v) for k,v in RESEARCH['parameters'].items()};params.update(tail_hours=0,trade_end=T+24.)
 rows=[];results={};common_fields=['net_usdt','funding_usdt','pair_usdt','fees_usdt']
 def summarize(r,name,t0,lo,cut,stop):
  offset=lo-t0;end=stop-t0;comp=r['comp'];prev=r['cum'][offset-1] if offset else 0.;delta=np.diff(np.r_[prev,r['cum'][offset:end]])
  cp=comp[offset-1] if offset else np.zeros(6);dc=comp[end-1]-cp
  pnl=float(delta.sum());fund=float(dc[0]);pair=float(dc[1]+dc[5]);fees=float(dc[2]);assert abs(pnl-(fund+pair-fees))<1e-6
  acc=r['accounts'];joint=acc[:,0]-acc[:,4]+acc[:,1]+acc[:,2]-S;err=float(np.max(abs(joint-r['cum'])));assert err<.002
  allsc=pd.DataFrame(D.arr[1]).where(lambda x:x>0).ffill().to_numpy();qty=np.divide(r['pos'][end-1],allsc[stop-1],out=np.zeros(N),where=allsc[stop-1]>0)
  startpos=0. if offset==0 else float(r['pos'][offset-1].sum());startnum=0 if offset==0 else int(np.count_nonzero(r['pos'][offset-1]>1e-12))
  # Under frozen no-delayed-leg engine, spot sold and perp quantity are paired;
  # a remaining recorded position keeps the completed return inapplicable.
  terminal_n=int(np.count_nonzero(qty>1e-12));H=stop-lo;annual=pnl/S*8760/H*100
  row={'case':name,'report_start_utc':str(ctx['START']+pd.Timedelta(hours=lo)),'entry_end_exclusive_utc':str(ctx['START']+pd.Timedelta(hours=cut)),
    'report_end_exclusive_utc':str(ctx['START']+pd.Timedelta(hours=stop)),'hours':H,'net_usdt':pnl,'funding_usdt':fund,'pair_usdt':pair,'fees_usdt':fees,
    'realized_pair_usdt':float(dc[1]),'unrealized_pair_change_usdt':float(dc[5]),
    'spread_impact_diagnostic_included_in_pair_usdt':float(dc[3]),'annual_marked_pct':annual,
    'annual_completed_pct':annual if terminal_n==0 else None,'terminal_open_symbols':terminal_n,'terminal_marked_position_usdt':float(r['pos'][end-1].sum()),
    'starting_open_symbols':startnum,'starting_marked_position_usdt':startpos,'ledger_identity_max_error_usdt':err,
    'resolved_engine_parameters':r['resolved_engine_parameters'],'real_execution_verified':False}
  dates=pd.date_range(ctx['START']+pd.Timedelta(hours=lo),periods=H,freq='h')
  cp0=comp[offset-1] if offset else np.zeros(6);allcomp=comp[offset:end]-cp0
  frame=pd.DataFrame({'hour_utc':dates,'net_pnl_usdt':delta,'cum_net_pnl_usdt':np.cumsum(delta),
   'funding_net_usdt':np.diff(np.r_[0,allcomp[:,0]]),'pair_price_usdt':np.diff(np.r_[0,allcomp[:,1]+allcomp[:,5]]),
   'fees_usdt':np.diff(np.r_[0,allcomp[:,2]]),'position_mtm_usdt':r['pos'][offset:end].sum(axis=1)})
  frame.to_csv(OUT/'curves'/f'{name}.csv',index=False);results[name]=frame
  rows.append(row);print(json.dumps({k:row[k] for k in ['case','net_usdt','annual_marked_pct','annual_completed_pct','terminal_open_symbols']}),flush=True)
  return row
 for name,start,end,warm,unwind in [
  ('late_continuous_open','2026-10-09','2026-11-08',True,False),
  ('late_cold_open','2026-10-09','2026-11-08',False,False),
  ('early_continuous_open','2026-09-28','2026-10-28',True,False),
  ('early_cold_open','2026-09-28','2026-10-28',False,False),
  ('early_cold_unwind','2026-09-28','2026-10-28',False,True),
  ('early_continuous_unwind','2026-09-28','2026-10-28',True,True)]:
  lo=RA.HABS(start);cut=RA.HABS(end);t0=RA.A0 if warm else lo
  RR.WINS[name]=(t0,cut);ex=dict(params)
  if unwind:ex['tail_hours']=216
  r=V.run_fix('cons_c0',name,extra=ex,oi_frac=float(P['oi_cap_fraction']),m=float(P['income_cost_multiple']))
  stop=cut+216 if unwind else cut;assert len(r['cum'])==stop-t0
  summarize(r,name,t0,lo,cut,stop)
  if unwind:summarize(r,name+'_at_cutoff',t0,lo,cut,cut)
  del r
 by={r['case']:r for r in rows};parity=[]
 for label,case in [('continuous','late_continuous_open'),('cold_start','late_cold_open')]:
  o=next(x for x in old['cases'] if x['label']==label);n=by[case]
  errors={'pnl':abs(n['net_usdt']-o['net_marked_pnl_usdt']),'funding':abs(n['funding_usdt']-o['funding_net_usdt']),'pair':abs(n['pair_usdt']-o['pair_price_including_impact_usdt']),'fees':abs(n['fees_usdt']-o['fee_usdt'])}
  assert max(errors.values())<1e-6,(case,errors)
  parity.append({'case':case,'component_errors_usdt':errors,'original_hourly_path_check':'See original local reconciliation; public package preserves original aggregate oracles only'})
 n=by['early_cold_unwind'];errors={k:abs(n[a]-target[b]) for k,a,b in [('pnl','net_usdt','net_usdt'),('funding','funding_usdt','funding_net_usdt'),('pair','pair_usdt','pair_price_including_impact_usdt'),('fees','fees_usdt','fees_usdt')]}
 assert max(errors.values())<1e-6,('new_M30_parity',errors)
 parity.append({'case':'early_cold_unwind','component_errors_usdt':errors,'original_hourly_path_check':'See original local reconciliation; public package preserves original aggregate oracles only'})
 # End policy must leave the path exactly unchanged before its announced cutoff.
 before=720-24
 unchanged={k:float(np.max(abs(results['early_cold_open'][k][:before]-results['early_cold_unwind'][k][:before]))) for k in ['net_pnl_usdt','position_mtm_usdt']}
 assert max(unchanged.values())<1e-6,unchanged
 # Ordered bridge; PnL components use a common30d scale until the explicit last step.
 steps=[];names=['late_continuous_open','late_cold_open','early_cold_open','early_cold_unwind_at_cutoff','early_cold_unwind']
 labels=['起始状态：连续持仓→空仓','日期：10/09–11/08→09/28–10/28','有限窗口：最后24小时启动锁退','追加216小时退出阶段损益']
 def on30(row):return row['net_usdt']/S*8760/720*100
 for i,label in enumerate(labels):
  a,b=by[names[i]],by[names[i+1]]
  change={k:b[k]-a[k] for k in common_fields};assert abs(change['net_usdt']-(change['funding_usdt']+change['pair_usdt']-change['fees_usdt']))<1e-6
  steps.append({'change':label,'from':a['case'],'to':b['case'],'before_ann_30d_pct':on30(a),'after_ann_30d_pct':on30(b),'annualized_difference_pp':on30(b)-on30(a),**{k+'_difference':v for k,v in change.items()},'elapsed_denominator_hours':720})
 a=by['early_cold_unwind'];steps.append({'change':'年化分母：30天→39天','from':'tail_pnl_common30d_scale','to':'official_complete39d',
  'before_ann_30d_pct':on30(a),'after_ann_39d_pct':a['annual_marked_pct'],'annualized_difference_pp':a['annual_marked_pct']-on30(a),
  'net_usdt_difference':0.,'funding_usdt_difference':0.,'pair_usdt_difference':0.,'fees_usdt_difference':0.,'elapsed_denominator_hours':936})
 total=sum(x['annualized_difference_pp'] for x in steps);expected=target['ann_completed_pct']-old['cases'][0]['simple_annualized_marked_pct'];assert abs(total-expected)<1e-10
 # Alternative ordering, averaged over date/state changes, shows interactions rather
 # than presenting one order as a uniquely identifiable market causal effect.
 A,B,C,Dd=[by[x] for x in ['late_continuous_open','late_cold_open','early_continuous_open','early_cold_open']]
 shapley={'initial_state_effect_pp':.5*((on30(B)-on30(A))+(on30(Dd)-on30(C))),
  'date_window_effect_pp':.5*((on30(C)-on30(A))+(on30(Dd)-on30(B))),
  'note':'two-factor average order only, under marked/no forced-end comparison; not a structural causal market model'}
 save('RESULTS.json',{'protocol':protocol,'cases':rows,'bridge':steps,'total_difference_pp':total,'old_new_parameter_differences':{
  k:[old['cases'][0]['engine_resolved_parameters'].get(k),target['resolved_engine_parameters'].get(k)] for k in set(old['cases'][0]['engine_resolved_parameters'])|set(target['resolved_engine_parameters']) if old['cases'][0]['engine_resolved_parameters'].get(k)!=target['resolved_engine_parameters'].get(k)},'reproduction_parity':parity,'identical_prefix_before_end_policy':unchanged,'date_state_average_order_attribution':shapley,'data_prefix_arrays_equal':manifest['old_prefix_arrays_identical'],'all_checks_passed':True})
 pd.DataFrame([{k:v for k,v in x.items() if k!='resolved_engine_parameters'} for x in rows]).to_csv(OUT/'SUMMARY.csv',index=False)
 pd.DataFrame(steps).to_csv(OUT/'BRIDGE.csv',index=False)
 save('VALIDATION.json',{'passed':True,'original_results_reproduced':3,'controlled_engine_runs':6,'reported_states':len(rows),'original_aggregate_endpoints_matched':True,'component_bridge_closes':True,'capital_and_inventory_same':True,'source_code_and_model_unchanged':True,'no_future_Nov17_data_fabricated':True})
 print('RECONCILED',total,flush=True)
if __name__=='__main__':
 try:main()
 except Exception:
  (OUT/'FAILURE.txt').write_text(traceback.format_exc());raise
