"""Frozen RC2 research economics, open-ended reporting cutoff, two starting states."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,os,sys,time
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1];ONLINE=ROOT.parent/'neg-funding-strategy-online-20261108';FROZEN=ONLINE/'frozen_reference'
PROFILE=json.loads((ONLINE/'runtime/MODEL_PROFILE.json').read_text());P=PROFILE['profile'];RESEARCH=P['historical_replay_profile']
CAPS=ROOT.parent.parent/'neg-funding-strategy-v1/private/inventory_caps.csv';DATA=ROOT/'data'
os.environ.update(NFB_DATA=str(DATA),NFB_DATA_DIR=str(DATA),NFB_DATA_START='2025-07-01',NFB_DATA_END='2026-11-08',
    NFB_CAP_FILE=str(CAPS),NFB_ROOT=str(ROOT/'audit/replay-scratch'),NFB_OUT=str(ROOT/'audit/replay-scratch')+'/',
    NFB_DAILY=str(ROOT/'audit/replay-scratch/daily')+'/',NFB_PHASE1_ROOT=str(ROOT),NFB_S='10000000',NFB_DERISK='2')
sys.path.insert(0,str(FROZEN/'repro/lib'));sys.path.insert(0,str(FROZEN/'research'))
import v13lib as V,run_r7 as RR,r7_lib as R7,run_adapt as RA,run_signals as RS
import engine_phase1 as XE,harness_phase1 as HB
from causal_inputs import causal_sigma
sys.path.insert(0,str(ONLINE/'runtime'));from online_scorer import Scorer
S=1e7;START=pd.Timestamp('2025-07-01',tz='UTC');M0=pd.Timestamp('2026-10-09',tz='UTC');M1=pd.Timestamp('2026-11-08',tz='UTC')
D=RR.D;T=len(D.arr[0]);N=D.N;lo=RA.HABS(M0);end=RA.HABS(M1);assert end==T and T-lo==720
def write(p,v):Path(p).write_text(json.dumps(v,indent=2,allow_nan=False)+'\n')
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def setup():
    HB.E=XE;HB.mkp=XE.mkp;HB.MKO=V._ns['MKO'];HB.MKO.clear();HB.MKO.update({k:float(v) for k,v in RESEARCH['fees'].items()})
    def phase_run(*a,**kw):kw['same_hour']=False;kw['dl_stop']=True;return HB.run(*a,**kw)
    RR.run=phase_run;D.oi_cap_fraction=float(P['oi_cap_fraction']);D.entry_score=None
    D._sg=causal_sigma(D.arr[1]);R7.SIGD=D._sg;RR.SIGD=D._sg
    with np.load(DATA/'ds/dataset.npz') as z:D._iv=z['ivh'].copy()
    registry=json.loads((FROZEN/'research/lifecycle_events.json').read_text())
    D.dl_exit=D.dl_exit.copy();D.dl_fin=D.dl_fin.copy()
    for ev in registry['events']:
        for sym in ev['symbols']:
            if sym in D.fut:
                n=D.fut.index(sym);D.dl_exit[n]=RA.HABS(ev['available_for_hourly_decision_utc']);D.dl_fin[n]=RA.HABS(ev['futures_delivery_utc'])
    doc=json.loads((ONLINE/'runtime/models/r7-model-bundle.json').read_text());scorer=Scorer(doc)
    # Read frozen monthly means, including the existing Nov1 update. No new fitting.
    income=np.full((T,N),np.nan);fb=RA.BKT;bb=R7.BBKT
    for model in doc['bundle']['income_models']:
        a=max(0,int((model['valid_from_ms']-START.timestamp()*1000)/3600000));b=min(T,int((model['valid_until_ms']-START.timestamp()*1000)/3600000))
        if b<=a:continue
        for j in range(4):
            for k in range(3):
                val=model['joint'][j][k]['mean']
                if val is None:
                    f=model['funding'][j]['mean'];bs=model['basis'][k]['mean'];val=f+bs if f is not None and bs is not None else np.nan
                block=income[a:b];mask=(fb[a:b]==j)&(bb[a:b]==k);block[mask]=val
    # Independent comparison with frozen vector tables over their original dates.
    original=R7.exp_income(True);m=(np.arange(T)[:,None]>=RA.A0)&(np.arange(T)[:,None]<RA.A1)&np.isfinite(original)
    maxerr=float(np.max(abs(income[m]-original[m])));assert maxerr<1e-12,maxerr
    R7.exp_income=lambda basis=True:income
    RR.gate_r7=lambda multiple,Y,H,basis=True:np.isfinite(income)&(income>=multiple*R7.entry_cost(Y,H))
    # Check representative November scalar values against the frozen bundle.
    checks=[]
    for t in [RA.HABS('2026-11-01'),RA.HABS('2026-11-06'),T-1]:
        for n in range(0,N,17):
            now=int(START.timestamp()*1000)+t*3600000
            basis=RA.b_prev[t,n];r8=RS.r8n[t,n]
            if not np.isfinite(basis) or not np.isfinite(r8):continue
            est,err=scorer.estimate(float(r8),float(basis),now)
            expected=np.nan if err else est['value']
            assert np.isclose(expected,income[t,n],equal_nan=True,atol=1e-12),('income',t,n)
            yc,hc,_=scorer.calibration(D.fut[n],now)
            assert abs(yc-RR.YK[t,n])<1e-12 and abs(hc-RR.HM[t,n])<1e-12
            checks.append((t,n))
    # Cost matrices first two components retain the exact RC2 calibration.
    write(ROOT/'audit/MODEL_PARITY.json',{'frozen_profile_sha256':PROFILE['profile_sha256'],'frozen_bundle_sha256':doc['bundle_sha256'],
        'legacy_income_max_error':maxerr,'november_scalar_income_cost_samples':len(checks),'new_model_fitted':False,
        'cost_calibration_last_day':doc['bundle']['cost_calibration_last_day'],'recent_cost_candidate_used':False,
        'report_cutoff_is_not_forced_liquidation':True,'data_model_not_live_fills':True})

def extract(r,label,t0):
    start=lo-t0;H=720;assert len(r['cum'])==T-t0
    comp=r['comp'];allcum=r['cum'];pos=r['pos'];acc=r['accounts']
    prev=allcum[start-1] if start else 0.;delta=np.diff(np.r_[prev,allcum[start:]])
    cprev=comp[start-1] if start else np.zeros(6);dc=np.diff(np.vstack([cprev,comp[start:]]),axis=0)
    assert np.max(abs(delta-(dc[:,0]+dc[:,1]-dc[:,2]+dc[:,5])))<1e-6
    qtyden=pd.DataFrame(D.arr[1]).where(lambda a:a>0).ffill().to_numpy()[lo:T]
    qty=np.divide(pos[start:],qtyden,out=np.zeros_like(pos[start:]),where=qtyden>0)
    cap=pd.read_csv(CAPS).set_index('fut_symbol');capq=np.array([cap.cap_qty_token.get(s,0) for s in D.fut])
    assert np.all(qty<=capq[None,:]+1e-7*np.maximum(1,capq[None,:]))
    joint=acc[:,0]-acc[:,4]+acc[:,1]+acc[:,2]-S;ledgererr=float(np.max(abs(joint-allcum)));assert ledgererr<.002
    dates=pd.date_range(M0,periods=720,freq='h');month_cum=np.cumsum(delta)
    marked_pos=pos[start:].sum(axis=1);out=pd.DataFrame({'hour_utc':dates.astype(str),'date_utc':dates.strftime('%Y-%m-%d'),
        'net_pnl_usdt':delta,'funding_net_usdt':dc[:,0],'realized_pair_price_including_impact_usdt':dc[:,1],
        'fee_usdt':dc[:,2],'spread_impact_diagnostic_usdt':dc[:,3],'unrealized_pair_change_usdt':dc[:,5],
        'paired_price_including_impact_usdt':dc[:,1]+dc[:,5],'cum_net_pnl_usdt':month_cum,
        'cost_used_usdt':comp[start:,4]*0,'position_mtm_usdt':marked_pos,'open_symbols':(qty>1e-12).sum(axis=1)})
    # Cost-based open principal is reconstructed from the account's live principal path only where recorded.
    # Marked position is the unambiguous reported usage series; avoid substituting cumulative turnover.
    out=out.drop(columns='cost_used_usdt')
    out.to_csv(ROOT/(label+'_hourly.csv'),index=False)
    daily=out.groupby('date_utc',sort=True).agg(net_pnl_usdt=('net_pnl_usdt','sum'),funding_net_usdt=('funding_net_usdt','sum'),
        realized_pair_price_including_impact_usdt=('realized_pair_price_including_impact_usdt','sum'),
        unrealized_pair_change_usdt=('unrealized_pair_change_usdt','sum'),paired_price_including_impact_usdt=('paired_price_including_impact_usdt','sum'),
        fee_usdt=('fee_usdt','sum'),spread_impact_diagnostic_usdt=('spread_impact_diagnostic_usdt','sum'),
        average_position_usdt=('position_mtm_usdt','mean'),peak_position_usdt=('position_mtm_usdt','max'),
        end_position_usdt=('position_mtm_usdt','last'),end_open_symbols=('open_symbols','last')).reset_index()
    daily['cum_net_pnl_usdt']=daily.net_pnl_usdt.cumsum();daily['daily_return_on_10m']=daily.net_pnl_usdt/S
    daily.to_csv(ROOT/(label+'_daily.csv'),index=False)
    equity=np.r_[0.,month_cum];dd=np.maximum.accumulate(equity)-equity
    mm=acc[start:]
    with np.load(DATA/'ds/dataset.npz') as z:mc=z['mark_close'][lo:T]
    fc=D.arr[4][lo:T];valid_mark=np.isfinite(mc)&(mc>0)&(fc>0)
    mark_unrealized=mm[:,2]+np.where(valid_mark,qty*(mc-fc),0).sum(axis=1)
    mark_notional=np.where(valid_mark,qty*mc,qty*np.nan_to_num(np.maximum(fc,0),nan=0)).sum(axis=1)
    margin=np.maximum(0,mark_notional-(mm[:,1]+mark_unrealized))
    firstqty=np.zeros(N) if start==0 else np.divide(pos[start-1],pd.DataFrame(D.arr[1]).where(lambda a:a>0).ffill().to_numpy()[lo-1],out=np.zeros(N),where=D.arr[1][lo-1]>0)
    endqty=qty[-1];held=np.flatnonzero(endqty>1e-12)
    terminal=[{'sym':D.fut[n],'qty_token':float(endqty[n]),'spot_liability_mark_usdt':float(pos[-1,n]),
        'last_spot_price':float(qtyden[-1,n]),'funding_latest_r8':float(RS.r8n[-1,n]) if np.isfinite(RS.r8n[-1,n]) else None} for n in held]
    write(ROOT/(label+'_terminal.json'),terminal)
    summary={'label':label,'start_utc':'2026-10-09','end_utc_exclusive':'2026-11-08','days':30,'hours':720,
        'initial_capital_usdt':S,'annualization_denominator_usdt':S,'net_marked_pnl_usdt':float(delta.sum()),
        'period_return_pct':float(delta.sum()/S*100),'simple_annualized_marked_pct':float(delta.sum()/S*365/30*100),
        'funding_net_usdt':float(dc[:,0].sum()),'pair_price_including_impact_usdt':float((dc[:,1]+dc[:,5]).sum()),
        'realized_pair_price_including_impact_usdt':float(dc[:,1].sum()),'unrealized_pair_change_usdt':float(dc[:,5].sum()),
        'fee_usdt':float(dc[:,2].sum()),'spread_impact_diagnostic_already_in_pair_usdt':float(dc[:,3].sum()),
        'max_hourly_drawdown_usdt':float(dd.max()),'max_drawdown_pct_of_10m':float(dd.max()/S*100),
        'average_marked_position_usdt':float(marked_pos.mean()),'peak_marked_position_usdt':float(marked_pos.max()),
        'starting_marked_position_usdt':float(pos[start-1].sum()) if start else 0.,'starting_open_symbols':int((firstqty>1e-12).sum()),
        'ending_marked_position_usdt':float(marked_pos[-1]),'ending_open_symbols':len(held),
        'completed_liquidated_annualized_pct':None if len(held) else float(delta.sum()/S*365/30*100),
        'period_profit_includes_open_position_mark_changes':True,'future_exit_cost_not_deducted':True,
        'positive_days':int((daily.net_pnl_usdt>0).sum()),'negative_days':int((daily.net_pnl_usdt<0).sum()),
        'monthly_identity_error_usdt':abs(float(delta.sum()-(dc[:,0]+dc[:,1]-dc[:,2]+dc[:,5]).sum())),
        'joint_ledger_max_error_usdt':ledgererr,'one_x_margin_shortfall_hours':int((margin>1e-6).sum()),
        'max_one_x_margin_gap_usdt':float(margin.max()),'A_negative_cash_hours':int((mm[:,0]<-.01).sum()),
        'margin_check_uses_mark_close_where_available':True,'held_coin_hours_missing_mark_close':int(((qty>1e-12)&~valid_mark).sum()),
        'B_negative_wallet_hours':int((mm[:,1]<-.01).sum()),'inventory_caps_passed':True,
        'engine_resolved_parameters':r['resolved_engine_parameters'],'real_execution_verified':False}
    return summary,daily

def main():
    setup();summaries=[];all_daily={};start=time.monotonic()
    params={k:float(v) for k,v in RESEARCH['parameters'].items()};params.update(tail_hours=0,trade_end=T+24.)
    for label,t0 in [('continuous',RA.A0),('cold_start',lo)]:
        RR.WINS[label]=(t0,T)
        r=V.run_fix('cons_c0',label,extra=params,oi_frac=float(P['oi_cap_fraction']),m=float(P['income_cost_multiple']))
        summary,daily=extract(r,label,t0);summaries.append(summary);all_daily[label]=daily.to_dict('records')
        print(json.dumps({k:summary[k] for k in ['label','net_marked_pnl_usdt','simple_annualized_marked_pct','ending_open_symbols']}),flush=True)
        del r
    docs={'created_at_utc':datetime.now(timezone.utc).isoformat(),'strategy_id':P['strategy_id'],
        'profile_sha256':PROFILE['profile_sha256'],'cases':summaries,'data_hashes':json.loads((DATA/'MANIFEST.json').read_text())['hashes'],
        'inventory_source_sha256':sha(CAPS),'replay_engine_sha256':sha(FROZEN/'research/engine_phase1.py'),
        'historical_inventory_is_static_supplied_snapshot':True,'current_full_orderbook_not_backtested':True,
        'runtime_seconds':round(time.monotonic()-start,1),'automatic_collection_enabled':False,
        'source_reference_strategy_unchanged':True,'daily':all_daily}
    write(ROOT/'RESULTS.json',docs)
if __name__=='__main__':main()
