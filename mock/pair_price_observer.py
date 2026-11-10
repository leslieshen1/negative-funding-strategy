"""Telemetry-only clone of frozen RC2; never patch the original engine."""
from pathlib import Path
import hashlib,importlib.util,json

def build(source:Path,out:Path):
 s=source.read_text();patches=[]
 def patch(a,b,count=1):
  nonlocal s
  assert s.count(a)==count,(a[:90],s.count(a),count)
  s=s.replace(a,b);patches.append({'anchor':a[:110],'count':count})
 patch('first_entry_mask=None, entry_score=None):','first_entry_mask=None, entry_score=None, coin_snapshots=None, snapshot_hours=None, price_trace=None, trace_count=None):')
 patch('    TT, N = sv.shape\n','''    TT, N = sv.shape
    obs_fund=np.zeros(N);obs_realized=np.zeros(N);obs_fees=np.zeros(N);obs_cost=np.zeros(N);obs_own=np.zeros(N)
    obs_spread=np.zeros(N);obs_impact=np.zeros(N);obs_ref_cash=np.zeros(N)
    obs_spread_s=np.zeros(N);obs_spread_f=np.zeros(N);obs_impact_s=np.zeros(N);obs_impact_f=np.zeros(N)
''')
 anchor='    episode_mode = np.zeros(N)'
 patch(anchor,'''    if mkm != 0 or mk_share != 0 or leg_dly != 0 or mirror_on != 0 or res_mode < 2.5 or sqrt_y <= 0 or slip_mode != 0 or tiered_exec != 0 or f_exitpov < 0.5:
        raise ValueError("Observer supports only frozen paired taker RC2 with square-root impact and real residuals")
'''+anchor)
 anchor='                    lost_f += (e2 - e) * mk_ * pos[n]; lf_ep[n] += (e2 - e) * mk_ * pos[n]; e = e2'
 patch(anchor,'                    obs_own[n]+=(e2-e)*mk_*pos[n]\n'+anchor)
 anchor='                fund_acc[n] += fnd; tf[li] += fnd'
 patch(anchor,anchor+'; obs_fund[n]+=fnd')
 anchor='            tp[li] += price; tfe[li] += fees; tc[li] += cost; cost_ep[n] += cost; ep_price[n] += price'
 telemetry='''
            obs_realized[n]+=price;obs_fees[n]+=fees;obs_cost[n]+=cost
            obs_spread_s[n]+=x*spot_mid*hs_sx;obs_spread_f[n]+=x*fut_mid*hs_fx
            obs_impact_s[n]+=x*spot_mid*sls;obs_impact_f[n]+=x*fut_mid*slf
            obs_spread[n]+=x*(spot_mid*hs_sx+fut_mid*hs_fx)
            obs_impact[n]+=x*(spot_mid*sls+fut_mid*slf)
            obs_ref_cash[n]+=x*(fut_mid-spot_mid)
            if price_trace is not None:
                obs_j=int(trace_count[0])
                if obs_j>=len(price_trace):raise ValueError("Observer trace buffer exhausted; preserve failure")
                price_trace[obs_j,0]=t;price_trace[obs_j,1]=n;price_trace[obs_j,2]=-1
                price_trace[obs_j,3]=x;price_trace[obs_j,4]=spot_mid;price_trace[obs_j,5]=fut_mid
                price_trace[obs_j,6]=sp_x;price_trace[obs_j,7]=fp_x
                price_trace[obs_j,8]=hs_sx;price_trace[obs_j,9]=hs_fx;price_trace[obs_j,10]=sls;price_trace[obs_j,11]=slf
                price_trace[obs_j,12]=fees;price_trace[obs_j,13]=1 if forced else 0
                price_trace[obs_j,14]=Q;price_trace[obs_j,15]=dv[t,n];price_trace[obs_j,16]=sg[t,n]
                price_trace[obs_j,17]=xa[0,t,n] if use_xa>0.5 else 1.0
                trace_count[0]+=1
'''
 patch(anchor,anchor+telemetry)
 anchor='                    fees_acc[n] += fees; tfe[li] += fees; tc[li] += cost; cost_ep[n] += cost'
 telemetry='''
                    obs_fees[n]+=fees;obs_cost[n]+=cost
                    obs_spread_s[n]+=x*spot_mid*hs_s[n]*spread_mult*hsm_;obs_spread_f[n]+=x*fut_mid*hs_f[n]*spread_mult*hsm_
                    obs_impact_s[n]+=x*spot_mid*sls;obs_impact_f[n]+=x*fut_mid*slf
                    obs_spread[n]+=x*(spot_mid*hs_s[n]+fut_mid*hs_f[n])*spread_mult*hsm_
                    obs_impact[n]+=x*(spot_mid*sls+fut_mid*slf)
                    obs_ref_cash[n]+=x*(spot_mid-fut_mid)
                    if price_trace is not None:
                        obs_j=int(trace_count[0])
                        if obs_j>=len(price_trace):raise ValueError("Observer trace buffer exhausted; preserve failure")
                        price_trace[obs_j,0]=t;price_trace[obs_j,1]=n;price_trace[obs_j,2]=1
                        price_trace[obs_j,3]=x;price_trace[obs_j,4]=spot_mid;price_trace[obs_j,5]=fut_mid
                        price_trace[obs_j,6]=sp_e;price_trace[obs_j,7]=fp_e
                        price_trace[obs_j,8]=hs_s[n]*spread_mult*hsm_;price_trace[obs_j,9]=hs_f[n]*spread_mult*hsm_
                        price_trace[obs_j,10]=sls;price_trace[obs_j,11]=slf
                        price_trace[obs_j,12]=fees;price_trace[obs_j,13]=0
                        price_trace[obs_j,14]=Q;price_trace[obs_j,15]=dv[t,n];price_trace[obs_j,16]=sg[t,n]
                        price_trace[obs_j,17]=xa[0,t,n] if use_xa>0.5 else 1.0
                        trace_count[0]+=1
'''
 patch(anchor,anchor+telemetry)
 anchor='        out_cum[t - t0] = tp[0] + tp[1] + tf[0] + tf[1] - tfe[0] - tfe[1] + mtm'
 patch(anchor,anchor+'''
        if coin_snapshots is not None:
            for obs_snap in range(len(snapshot_hours)):
                if t==snapshot_hours[obs_snap]:
                    for obs_n in range(N):
                        coin_snapshots[obs_snap,obs_n,0]=obs_fund[obs_n]
                        coin_snapshots[obs_snap,obs_n,1]=obs_realized[obs_n]
                        coin_snapshots[obs_snap,obs_n,2]=obs_fees[obs_n]
                        coin_snapshots[obs_snap,obs_n,3]=pos[obs_n]*side[obs_n]*(sp_avg[obs_n]-last_sc[obs_n]+last_fc[obs_n]-fp_avg[obs_n])
                        coin_snapshots[obs_snap,obs_n,4]=obs_cost[obs_n]
                        coin_snapshots[obs_snap,obs_n,5]=obs_own[obs_n]
                        coin_snapshots[obs_snap,obs_n,6]=obs_spread[obs_n]
                        coin_snapshots[obs_snap,obs_n,7]=obs_impact[obs_n]
                        coin_snapshots[obs_snap,obs_n,8]=obs_ref_cash[obs_n]
                        coin_snapshots[obs_snap,obs_n,9]=pos[obs_n]*(last_fc[obs_n]-last_sc[obs_n])
                        coin_snapshots[obs_snap,obs_n,10]=pos[obs_n]
                        coin_snapshots[obs_snap,obs_n,11]=pos[obs_n]*last_sc[obs_n]
                        coin_snapshots[obs_snap,obs_n,12]=obs_spread_s[obs_n]
                        coin_snapshots[obs_snap,obs_n,13]=obs_spread_f[obs_n]
                        coin_snapshots[obs_snap,obs_n,14]=obs_impact_s[obs_n]
                        coin_snapshots[obs_snap,obs_n,15]=obs_impact_f[obs_n]
''')
 target=out/'src/engine_price_observer.py';target.write_text(s)
 (out/'audit/ENGINE_PATCHES.json').write_text(json.dumps({'purpose':'Telemetry only; baseline controls checked separately',
  'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'observer_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
  'frozen_source_modified':False,'patches':patches},indent=2)+'\n')
 spec=importlib.util.spec_from_file_location('engine_price_observer',target);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
