"""Pinned baseline, causal corrections and labelled exploratory comparisons."""
from pathlib import Path
import argparse,hashlib,importlib,json,os,sys,time
import numpy as np
import pandas as pd

HERE=Path(__file__).resolve().parent;ROOT=Path(os.environ.get('NFB_PHASE1_ROOT',str(HERE.parents[1]))).resolve();REPO=HERE.parent
ap=argparse.ArgumentParser();ap.add_argument('--data',required=True);ap.add_argument('--suite',choices=['bridge','research','reference','lifecycle','followup','concentration'],default='bridge')
ap.add_argument('--output',required=True);ap.add_argument('--profile',default='reference');args=ap.parse_args()
profile_file=REPO/'profiles'/(args.profile+'.json')
PROFILE=json.loads(profile_file.read_text());PROFILE_HASH=hashlib.sha256(profile_file.read_bytes()).hexdigest()
if float(PROFILE['capital_usdt']) != 1e7 or float(PROFILE['annualization_denominator_usdt']) != 1e7:
 raise ValueError('This study is pinned to initial capital and denominator 10M USDT')
if PROFILE['first_entry_wait'] != 'none':
 raise ValueError('Select the labelled T-1h experiment; this profile runner does not silently change entry timing')
if float(PROFILE['income_gate_roundtrip_fee_allowance']) != .003:
 raise ValueError('The frozen income-gate formula uses a 0.003 allowance; change it as a separate model experiment')
DATA=Path(args.data).resolve();OUT=Path(args.output).resolve();OUT.mkdir(parents=True,exist_ok=True)
manifest=json.loads((DATA/'MANIFEST.json').read_text()) if (DATA/'MANIFEST.json').exists() else {'start_utc':'2025-10-01','end_utc_exclusive':'2026-11-02'}
caps=Path(os.environ.get('NFB_CAP_FILE',ROOT.parent.parent/'neg-funding-strategy-v1/private/inventory_caps.csv'))
os.environ.update(NFB_DATA=str(DATA),NFB_DATA_DIR=str(DATA),NFB_CAP_FILE=str(caps),NFB_ROOT=str(OUT),
 NFB_OUT=str(OUT/'scratch')+'/',NFB_DAILY=str(OUT/'scratch/daily')+'/',NFB_S='10000000',NFB_DERISK='2',
 NFB_DATA_START=manifest['start_utc'],NFB_DATA_END=manifest['end_utc_exclusive'])
sys.path.insert(0,str(REPO/'repro/lib'));sys.path.insert(0,str(HERE))
import v13lib as V,run_r7 as RR,r7_lib as R7,run_adapt as RA,lib12s as L,run_signals as RS
from acquire_phase1 import js
D=RR.D;N=D.N;T=RR.T;S=1e7
FEES={k:float(v) for k,v in PROFILE['fees'].items()}
V._ns['MKO'].clear();V._ns['MKO'].update(FEES)
with np.load(DATA/'ds/dataset.npz') as z:
 explicit_iv=z['ivh'].copy();mark_close=z['mark_close'].copy() if 'mark_close' in z else None
caps_frame=pd.read_csv(caps).set_index('fut_symbol')
capqty=np.array([caps_frame.cap_qty_token.get(s,0.) for s in D.fut])
close_filled=pd.DataFrame(D.arr[1]).where(lambda a:a>0).ffill().to_numpy()
state={'same_hour':True};results=[];all_curves={};start_time=time.monotonic()

def metrics(r,name,window,extra):
 H=len(r['cum']);net=float(r['cum'][-1]);diag=r['diag'];lo,hi=RR.WINS[window]
 if H>hi-lo:hi=lo+H
 qty=np.divide(r['pos'],close_filled[lo:hi],out=np.zeros_like(r['pos']),where=close_filled[lo:hi]>0)
 ratio=np.divide(qty,capqty[None,:],out=np.zeros_like(qty),where=capqty[None,:]>0)
 assert not (ratio>1.00001).any()
 comp=r['comp'];assert np.max(np.abs(r['cum']-(comp[:,0]+comp[:,1]-comp[:,2]+comp[:,5])))<.001
 equity=S+np.r_[0.,r['cum']];draw=np.maximum.accumulate(equity)-equity
 ep=r['ep'];coin=np.bincount(ep[:,0].astype(int),weights=ep[:,3]-ep[:,6],minlength=N) if len(ep) else np.zeros(N)
 row={'case':name,'profile_id':PROFILE['strategy_id'],'profile_file_sha256':PROFILE_HASH,'window':window,'net_usdt':net,'ann_mtm_pct':net/S*8760/H*100,
  'ann_completed_pct':net/S*8760/H*100 if diag[24]==0 and extra.get('res_mode',2)>2.5 else None,
  'hours':H,'closed_or_estimated_episodes':len(ep),'funding_net_usdt':float(r['funding']),
  'fees_usdt':float(r['fees']),'pair_price_including_impact_usdt':float(r['price']),
  'own_rate_impact_reduction_usdt':float(r['lost_f']) if 'lost_f' in r else None,
  'spread_impact_diagnostic_already_in_price_usdt':float(r['cost']),
  'average_cost_used_usdt':float(diag[15]/H),'peak_cost_used_usdt':float(diag[12]),
  'max_hourly_drawdown_usdt':float(draw.max()),'terminal_residual_notional_usdt':float(diag[8]),
  'terminal_residual_symbols':int(diag[24]),'terminal_is_estimated_close':extra.get('res_mode',2)<2.5,
  'inventory_cap_passed':True,'top5_closed_net_contribution_pct':float(np.sort(coin)[-5:].sum()/coin.sum()*100) if coin.sum()!=0 else None,
  'average_hold_hours':float((ep[:,2]-ep[:,1]).mean()) if len(ep) else None,
  'effective_overrides':extra,'actual_execution_verified':False,
  'resolved_engine_parameters':r.get('resolved_engine_parameters'),
  'research_suite':args.suite,'profile_is_base_for_labelled_overrides':name!='causal_reference'}
 account=r.get('accounts')
 if account is not None:
  unp=account[:,2].copy();notional=account[:,3].copy()
  if mark_close is not None:
   mc=mark_close[lo:hi];fc=D.arr[4][lo:hi]
   valid=np.isfinite(mc)&(mc>0)&(fc>0)
   unp+=np.where(valid,qty*(mc-fc),0.).sum(axis=1)
   notional=np.where(valid,qty*mc,qty*np.nan_to_num(np.maximum(fc,0),nan=0.)).sum(axis=1)
  joint=account[:,0]-account[:,4]+account[:,1]+account[:,2]-S
  err=float(np.max(np.abs(joint-r['cum'])));assert err<.002,err
  eq=account[:,1]+unp;free=np.maximum(0.,np.minimum(account[:,1],eq-notional))
  ba=np.diff(np.r_[0.,account[:,7]]);ab=np.diff(np.r_[0.,account[:,8]])
  before_wallet=account[:,1]+ba-ab;before_eq=before_wallet+unp
  before_free=np.maximum(0.,np.minimum(before_wallet,before_eq-notional))
  row['accounts']={'joint_ledger_max_error_usdt':err,
   'hour_end_margin_proxy':'mark close where available; futures close fallback; no intrahour simultaneous mark path',
   'one_x_initial_margin_shortfall_hours':int((eq+1e-6<notional).sum()),
   'one_x_initial_margin_max_shortfall_usdt':float(np.maximum(0.,notional-eq).max()),
   'max_effective_B_leverage':float(np.divide(notional,eq,out=np.zeros_like(eq),where=eq>0).max()),
   'B_to_A_transfers_exceeding_hour_end_free_margin_proxy':int((ba>before_free+.01).sum()),
   'A_negative_cash_hours':int((account[:,0]<-.01).sum()),'B_negative_wallet_hours':int((account[:,1]<-.01).sum()),
   'B_to_A_total_usdt':float(account[-1,7]),'A_to_B_total_usdt':float(account[-1,8]),
   'margin_tier_history_exact':False,'transfer_path_intra_hour_exact':False}
  natural_a=np.diff(np.r_[0.,account[:,0]])-ba+ab
  natural_b=np.diff(np.r_[S,account[:,1]])+ba-ab
  stress={}
  for delay in [1,4,8]:
   arrival_ba=np.r_[np.zeros(delay),ba[:-delay]];arrival_ab=np.r_[np.zeros(delay),ab[:-delay]]
   aa=np.cumsum(natural_a-ab+arrival_ba);bb=S+np.cumsum(natural_b-ba+arrival_ab)
   stress[str(delay)+'h']={'A_max_cash_gap_usdt':float(np.maximum(0,-aa).max()),
    'B_max_wallet_gap_usdt':float(np.maximum(0,-bb).max()),
    'B_one_x_margin_max_gap_usdt':float(np.maximum(0,notional-(bb+unp)).max()),
    'B_one_x_margin_shortfall_hours':int((bb+unp+1e-6<notional).sum()),
    'interpretation':'Fixed-path delayed-arrival diagnostic. Negative balances mean this path needs earlier transfers, a cash buffer, or changed fills; not executable PnL.'}
  row['transfer_delay_stress']=stress
  if name=='causal_reference' and window=='FY':
   pd.DataFrame({'A_cash':account[:,0],'B_wallet':account[:,1],'B_mark_unrealized':unp,
    'B_notional':notional,'coin_liability_value':account[:,4],'B_free_margin_proxy':free,
    'B_to_A_cumulative':account[:,7],'A_to_B_cumulative':account[:,8]}).to_csv(OUT/'account_audit_FY.csv',index=False)
 dates=pd.date_range(manifest['start_utc'],periods=hi,freq='h',tz='UTC')[lo:hi]
 curve=pd.DataFrame({'hour_utc':dates,'pnl_usdt':r['cum'],'position_mtm_usdt':r['used']})
 curve.to_csv(OUT/f'{name}_{window}_curve.csv',index=False)
 if window=='FY':all_curves[name]=np.diff(np.r_[0.,r['cum']])[:8760]
 if name in ['causal_reference','volume_cap_2pct','liq2_oi5','liq5_oi2p5'] and window=='FY':
  private=ROOT/'private';private.mkdir(exist_ok=True)
  position_name='reference_positions.npz' if name=='causal_reference' else name+'_positions.npz'
  np.savez_compressed(private/position_name,qty_token=qty,symbols=np.array(D.fut),start_hour=lo)
  (private/position_name).chmod(0o600)
  own_private=OUT/'private';own_private.mkdir(exist_ok=True)
  np.savez_compressed(own_private/position_name,qty_token=qty,symbols=np.array(D.fut),start_hour=lo)
  (own_private/position_name).chmod(0o600)
 row['runtime_since_start_seconds']=round(time.monotonic()-start_time,1)
 return row

def run(name,extra=None,mask=None,windows=('IS','HO','FY'),oi=None,rank=None,gate=None):
 if oi is None:oi=float(PROFILE['oi_cap_fraction'])
 if gate is None:gate=float(PROFILE['income_cost_multiple'])
 D.oi_cap_fraction=oi;D.entry_score=rank
 base=dict(entry_fav_n=.001);base.update(extra or {})
 for win in windows:
  r=V.run_fix('cons_c0',win,mask=mask,extra=base,m=gate)
  row=metrics(r,name,win,base);row['oi_cap_fraction']=oi;row['income_cost_multiple']=gate
  results.append(row);js(OUT/'results.json',{'cases':results,'source_profile':'frozen_v1_rule_family','data':manifest})
  print(json.dumps({k:row[k] for k in ['case','window','ann_mtm_pct','ann_completed_pct','terminal_residual_symbols','net_usdt']}),flush=True)
  del r

if args.suite=='bridge':
 run('native_0p1',windows=('FY',))
 assert abs(results[-1]['ann_mtm_pct']-75.16414173387383)<1e-9

import engine_phase1 as XE,harness_phase1 as HB
HB.E=XE;HB.mkp=XE.mkp;HB.MKO=V._ns['MKO']
def phase_run(*a,**kw):
 kw['same_hour']=state['same_hour']
 if args.suite in ['lifecycle','followup','reference','concentration'] and PROFILE.get('lifecycle_notices_enabled'):kw['dl_stop']=True
 return HB.run(*a,**kw)
RR.run=phase_run
if args.suite=='bridge':
 run('extended_engine_legacy_compatibility',windows=('FY',))
 for k in ['net_usdt','funding_net_usdt','fees_usdt','pair_price_including_impact_usdt']:
  assert abs(results[-1][k]-results[-2][k])<1e-6,(k,results[-1][k],results[-2][k])

# Missing estimates use only the available cross-section and preceding hours.
from causal_inputs import causal_sigma
D._sg=causal_sigma(D.arr[1]);R7.SIGD=D._sg;RR.SIGD=D._sg
D._iv=explicit_iv
for s in ['cal','book','sqrt1']:
 RR.XA[s]=RR.xa_for(s);RR.XA[s][0][2]=R7.ev_ratio(RR.XA[s][1],RR.XA[s][2],True)
if args.suite=='bridge':run('causal_sigma_and_zero_settlements',windows=('FY',))
state['same_hour']=False
if args.suite=='bridge':run('prior_budget_and_raw_fill_cap',dict(f_prior=1.,fill_cap=1.),windows=('FY',))
reference={k:float(v) for k,v in PROFILE['parameters'].items()}
if args.suite in ['lifecycle','followup','reference','concentration'] and PROFILE.get('lifecycle_notices_enabled'):
 registry=json.loads((HERE/'lifecycle_events.json').read_text())
 D.dl_exit=D.dl_exit.copy();D.dl_fin=D.dl_fin.copy()
 for event in registry['events']:
  for symbol in event['symbols']:
   if symbol in D.fut:
    n=D.fut.index(symbol);D.dl_exit[n]=RA.HABS(event['available_for_hourly_decision_utc']);D.dl_fin[n]=RA.HABS(event['futures_delivery_utc'])
run('causal_reference',reference)

if args.suite=='concentration':
 for name,ex in [('cost_cap_only',{'per_coin_mtm_fraction':0.}),
                 ('per_coin_20pct',{'per_coin_cap_fraction':.2,'per_coin_mtm_fraction':.2}),
                 ('double_execution_cost',{'sqrt_y':2.,'spread_mult':2.}),
                 ('own_funding_k6',{'k_imp':6.}),
                 ('combined_cost2_k6',{'sqrt_y':2.,'spread_mult':2.,'k_imp':6.})]:
  run(name,dict(reference,**ex))

if args.suite=='followup':
 run('volume_cap_2pct',dict(reference,vol_frac=.02))
 for name,ex,oi in [('liq2_oi5',{'vol_frac':.02},.05),('liq5_marginal',{'vol_frac':.05,'imp_size':1.},.1),
   ('liq5_oi2p5',{'vol_frac':.05},.025),('liq2_cost2',{'vol_frac':.02,'sqrt_y':2.,'spread_mult':2.},.1),
   ('liq2_k6',{'vol_frac':.02,'k_imp':6.},.1),
   ('liq5oi2p5_cost2',{'vol_frac':.05,'sqrt_y':2.,'spread_mult':2.},.025),
   ('liq5oi2p5_cost2_k6',{'vol_frac':.05,'sqrt_y':2.,'spread_mult':2.,'k_imp':6.},.025),
   ('liq5oi2p5_k6',{'vol_frac':.05,'k_imp':6.},.025)]:run(name,dict(reference,**ex),oi=oi)
 import entry_policy
 max_jitter=float(np.max(np.abs(explicit_iv-np.rint(explicit_iv))))
 assert max_jitter<=1/60.,'Non-hourly intervals need an event-time model'
 policy=entry_policy.settled_entry_policy(RS.ev,np.rint(explicit_iv),add_timing='qualified')
 js(OUT/'T1_POLICY.json',dict(policy.metadata,schedule_interval_rounding_max_seconds=max_jitter*3600.,
  schedule_source='latest settled interval continuation; hourly schedule rounds sub-minute timestamp jitter, not a historically received schedule'))
 D.entry_recheck_mask=policy.mask
 run('first_Tminus1_only',reference)
 run('liq2_first_Tminus1',dict(reference,vol_frac=.02))
 D.entry_recheck_mask=None

if args.suite in ['research','lifecycle']:
 # Small predefined set, no Cartesian sweep or full-year best-parameter claim.
 cases=[('entry_0p05',{'entry_fav_n':.0005}),('entry_0p2',{'entry_fav_n':.002}),
  ('latest_rate_exit',{'xmode':8.}),('positive_rate_exit',{'xmode':6.,'x6_thr':0.}),
  ('exit_confirm_6h',{'conf_h':6.}),('marginal_impact_size',{'imp_size':1.}),
  ('cost_stress_1p5',{'sqrt_y':1.5,'spread_mult':1.5}),('cost_stress_2',{'sqrt_y':2.,'spread_mult':2.}),
  ('own_funding_k2',{'k_imp':2.}),('own_funding_k6',{'k_imp':6.}),
  ('single_coin_cap_10pct',{'per_coin_cap_fraction':.1})]
 if args.suite=='lifecycle':
  cases=[('entry_0p05',{'entry_fav_n':.0005}),('entry_0p2',{'entry_fav_n':.002}),
   ('latest_rate_exit',{'xmode':8.}),('positive_rate_exit',{'xmode':6.,'x6_thr':0.}),
   ('exit_confirm_6h',{'conf_h':6.}),('single_coin_cap_10pct',{'per_coin_cap_fraction':.1}),
   ('marginal_impact_size',{'imp_size':1.}),
   ('volume_cap_5pct',{'vol_frac':.05}),('volume_cap_2pct',{'vol_frac':.02}),
   ('cost_stress_2',{'sqrt_y':2.,'spread_mult':2.}),('own_funding_k6',{'k_imp':6.})]
 for name,ex in cases:run(name,dict(reference,**ex))
 for gate in [1.,3.]:run(f'income_cost_multiple_{int(gate)}',reference,gate=gate)
 for oi in [.025,.05]:run('OI_cap_'+str(oi),reference,oi=oi)
 # Same income model and gate; only candidate ordering changes.
 ei=R7.exp_income(True);cost=R7.entry_cost(RR.YK,RR.HM)
 rank=np.divide(ei-cost,cost,out=np.full_like(cost,np.nan),where=cost>0)
 run('rank_expected_net_per_cost',reference,rank=rank)
 # Capacity is a deployment ceiling; all returns still divide by initial 10M.
 for frac in ([.5] if args.suite=='lifecycle' else [.1,.25,.5]):run('deployment_'+str(frac),dict(reference,deployment_fraction=frac))
 # Training horizon is a model change, separated from execution/data fixes.
 original=(R7.F24,R7.END24,R7.FIN24,R7.DB24,R7.EF_T,R7.EB_T,R7.E2_T)
 R7.F24,R7.END24,R7.FIN24=RA.sample_outcomes(1)
 end=np.minimum(R7.END24,T-1);R7.DB24=np.take_along_axis(R7.bnow,end,axis=0)-R7.b_prev
 R7.EF_T,R7.EB_T,R7.E2_T=R7.ei_tables()
 run('income_training_matches_exit',reference)
 R7.F24,R7.END24,R7.FIN24,R7.DB24,R7.EF_T,R7.EB_T,R7.E2_T=original

reference_curve=all_curves['causal_reference'];rng=np.random.default_rng(20261106)
weeks=[np.arange(i,min(i+168,8760)) for i in range(0,8760,168)]
weekly={k:np.array([v[idx].sum() for idx in weeks]) for k,v in all_curves.items()}
draws=rng.integers(0,len(weeks),size=(2000,len(weeks)));uncertainty={}
for k,v in weekly.items():
 diff=v-weekly['causal_reference'];sample=diff[draws].sum(axis=1)/S*100
 uncertainty[k]={'FY_common_8760h_difference_pct':float(diff.sum()/S*100),
  'weekly_block_resample_95pct_interval_pct':list(map(float,np.quantile(sample,[.025,.975]))),
  'note':'Exploratory, same already-used historical year; not a new untouched holdout or correction for multiple testing. Tail PnL excluded from this common-window comparison.'}
js(OUT/'weekly_uncertainty.json',uncertainty)
js(OUT/'VALIDATION.json',{'completed':True,'native_75p16_checked':args.suite=='bridge',
 'causal_budget_uses_prior_hour':True,'raw_current_volume_only_limits_fills':True,
 'native_strategy_rules_retained_except_labelled_overrides':True,'fixed_denominator_usdt':S,
 'new_execution_engine_built':False,'actual_execution_verified':False,'elapsed_seconds':round(time.monotonic()-start_time,1)})
print('PASS: study complete',flush=True)
