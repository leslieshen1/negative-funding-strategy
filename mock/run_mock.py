"""Sequential synthetic exchange mock; no network, orders or real forecast ledger.

The policy is a separate process and never receives the random seed, future
prices, future settlement rates, execution draws or the evaluator's assertions.
Historical replay mode is explicit. Synthetic PnL is not a return forecast.
"""
from pathlib import Path
from decimal import Decimal as D,ROUND_FLOOR
from datetime import datetime,timezone
from collections import Counter
import argparse,copy,hashlib,json,os,random,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
H=3600000
BASE=1794182400000 # 2026-11-09 00UTC, virtual synthetic clock
CAPITAL=D('10000000')
FAMILIES=['normal','partial_fills','one_leg_delay','stale_quotes','exit_lock_rebound',
          'borrow_recall','liquidity_collapse','cash_transfer_delay']

def s(v):return format(D(v),'f')
def quant(v,step):return (max(D(0),D(v))/step).to_integral_value(rounding=ROUND_FLOOR)*step
def canon(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def sha(x):return hashlib.sha256(x).hexdigest()

class Worker:
    def __init__(self):
        self.p=subprocess.Popen([sys.executable,'-B',str(ROOT/'mock/policy_worker.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    def ask(self,request,clock):
        self.p.stdin.write(canon({'request':request,'clock_ms':clock})+'\n');self.p.stdin.flush()
        line=self.p.stdout.readline()
        if not line:raise RuntimeError('Policy process stopped: '+self.p.stderr.read()[-2000:])
        return json.loads(line)
    def close(self):
        self.p.stdin.close();self.p.wait(timeout=30)
        if self.p.returncode:raise RuntimeError(self.p.stderr.read()[-2000:])

class Exchange:
    def __init__(self,seed,family,index,template,prior):
        self.rng=random.Random(seed);self.family=family;self.index=index
        self.identifier=sha(str(seed).encode())[:16]
        self.template=template;self.prior=prior
        self.mult=D(str(prior['tokens_per_perp_quantity']))
        self.step=D('1') if self.mult==1000 else D('.01')
        self.inventory=quant(D('30000')/D('1'),self.step)
        self.owned=self.inventory;self.debt=self.inventory
        self.a=D(0);self.b=CAPITAL;self.sold=D(0);self.perp=D(0)
        self.spot_entry=D(0);self.perp_entry=D(0);self.entry_cost=D(0)
        self.exit_locked=False;self.episode=None;self.pending=None
        self.funding=D(0);self.fees=D(0);self.repairs=0;self.transfer_count=0
        self.slices=Counter();self.actions=Counter();self.violations=[]
        self.bars=[];self.events=[];self.trace=[];self.entered=False
        self.receipts=[];self.prefix_probe=None
        self.make_world()
    def make_world(self):
        # Private to the exchange. The worker only sees a past slice at each step.
        self.world=[];price=1.0
        for k in range(-32,145):
            drift=.009 if self.family=='cash_transfer_delay' and 0<k<65 else 0.
            price*=1+drift+self.rng.gauss(0,.0015)
            basis=-.007 if k<44 else -.0015
            if self.family=='exit_lock_rebound' and k>=80:basis=-.007
            basis+=self.rng.uniform(-.00015,.00015)
            rate=-.003 if k<48 else .0008
            if self.family=='exit_lock_rebound' and k>=80:rate=-.003
            volume=100000.
            if self.family=='liquidity_collapse' and k>=48:volume=40.
            self.world.append({'k':k,'spot':D(str(round(price,8))),'perp':D(str(round(price*(1+basis),8))),
                 'rate':D(str(rate)),'volume':D(str(volume)),
                 'fill_fraction':D(str(self.rng.choice([.25,.5,.75,1.]) if self.family in ['partial_fills','one_leg_delay'] else 1.)),
                 'partial_leg':self.rng.choice(['spot','perp'])})
    def row(self,k):return self.world[k+32]
    def event(self,k):
        w=self.row(k);now=BASE+k*H
        if k%8==0:
            self.events.append({'funding_time_ms':now,'received_at_ms':now,'raw_rate':s(w['rate']),'interval_hours':'8'})
            paid=-self.perp*w['perp']*w['rate'];self.b+=paid;self.funding+=paid
            self.receipts.append({'kind':'funding','at_ms':now,'amount_usdt':s(paid),'origin':'synthetic'})
        if self.pending and k>=self.pending['arrival']:
            self.a+=self.pending['amount'];self.b-=self.pending['amount']
            self.receipts.append({'kind':'internal_transfer','at_ms':now,'amount_usdt':s(self.pending['amount']),'origin':'synthetic'})
            self.pending=None;self.transfer_count+=1
    def repair(self,k):
        if self.sold==self.perp:return
        w=self.row(k);q=abs(self.sold-self.perp)
        # Explicit mock executor policy: remove the unmatched exposure next tick.
        # This is not a claim that the strategy decisioner implemented leg repair.
        if self.sold>self.perp:
            q=min(q,quant(self.a/(w['spot']*D('1.0009')),self.step))
            if q:self.fill('spot','BUY',q,w['spot']*D('1.0003'),k,repair=True)
        else:self.fill('perp','SELL',q,w['perp']*D('.9997'),k,repair=True)
        self.repairs+=1
    def request(self,k):
        now=BASE+k*H;w=self.row(k);req=copy.deepcopy(self.template)
        req.update(request_id=f'mock-{self.identifier}-{k}',as_of_ms=now,ledger_version=k+1,mode='paper')
        row=req['instruments'][0];i=row['instrument'];p=row['position'];m=row['market'];risk=row['risk']
        symbol=self.prior['perp_symbol'];spot=self.prior['spot_symbol'];token=spot.removesuffix('USDT')
        iid=spot+'-'+symbol
        i.update(instrument_id=iid,token=token,spot_symbol=spot,perp_symbol=symbol,tokens_per_perp_quantity=s(self.mult),
           spot_qty_step_token=s(self.step),perp_qty_step_exchange=s(self.step/self.mult),
           spot_min_qty_token=s(self.step),perp_min_qty_exchange=s(self.step/self.mult),spot_min_notional_usdt='1',perp_min_notional_usdt='1')
        unreal=self.perp*(w['perp']-self.perp_entry)
        pnl=self.a+self.b+(self.owned-self.debt)*w['spot']+unreal-CAPITAL
        notional=self.perp*w['perp'];free=max(D(0),min(self.b-notional,self.b+unreal-notional))
        if self.pending:free=max(D(0),free-self.pending['amount'])
        req['portfolio'].update(strategy_marked_pnl_usdt=s(pnl),used_entry_capital_usdt=s(self.entry_cost),
          reserved_entry_capital_usdt='0',account_a_free_usdt=s(max(D(0),self.a)),account_b_free_usdt=s(free),
          account_b_wallet_usdt=s(self.b),account_b_equity_usdt=s(self.b+unreal),account_b_initial_margin_1x_usdt=s(notional),
          account_b_position_notional_usdt=s(notional),account_b_transferable_usdt=s(free),
          unsettled_transfer_usdt=s(self.pending['amount']) if self.pending else '0',snapshot_received_at_ms=now,risk_state='NORMAL')
        p.update(strategy_sold_spot_qty_token=s(self.sold),perp_long_qty_token=s(self.perp),perp_long_qty_exchange=s(self.perp/self.mult),
          exit_locked=self.exit_locked,episode_id=self.episode,entry_cost_usdt=s(self.entry_cost),reserved_entry_cost_usdt='0',reserved_add_qty_token='0',
          episode_coin_return_due_token=s(self.sold))
        history=[{'bar_end_ms':BASE+j*H,'received_at_ms':BASE+j*H,'spot_close_usdt_per_token':s(self.row(j)['spot']),
          'perp_close_usdt_per_token':s(self.row(j)['perp']),'oi_usdt':'5000000','availability_source':'research_hourly_receive_proxy'} for j in range(k-25,k+1)]
        settled=[e for e in self.events if now-24*H<e['funding_time_ms']<=now]
        latest=max(self.events,key=lambda e:e['funding_time_ms'])
        m.update(status='OK',observed_at_ms=now,previous_bar_end_ms=now,spot_close_usdt_per_token=s(w['spot']),perp_close_usdt_per_token=s(w['perp']),
           settled_r8=latest['raw_rate'],settled_r8_source='latest_settled',latest_settlement_time_ms=latest['funding_time_ms'],
           latest_settlement_received_at_ms=latest['received_at_ms'],latest_raw_rate=latest['raw_rate'],latest_interval_hours='8',
           raw_settled_sum_24h=s(sum((D(e['raw_rate']) for e in settled),D(0))),settlement_history_complete=True,
           open_interest_usdt='5000000',trailing_24h_paired_quote_volume_usdt=s(sum((self.row(j)['volume'] for j in range(k-23,k+1)),D(0))),
           capacity_window_end_ms=now,capacity_window_start_ms=now-H,capacity_window_complete=True,capacity_view='causal_online',
           spot_quote_volume_capacity_window_usdt=s(w['volume']),perp_quote_volume_capacity_window_usdt=s(w['volume']),
           depth_0_2pct_usdt='10000',depth_1pct_usdt='50000',depth_source='volume_proxy',quotes_received_at_ms=now,
           completed_inputs_received_at_ms=now,shared_slice_used_usdt='0',shared_slice_reserved_usdt='0',settlement_coverage_start_ms=now-32*H,
           spot_bid_usdt_per_token=s(w['spot']*D('.9997')),spot_ask_usdt_per_token=s(w['spot']*D('1.0003')),
           perp_bid_usdt_per_token=s(w['perp']*D('.9997')),perp_ask_usdt_per_token=s(w['perp']*D('1.0003')),settlements=settled)
        row['limits'].update(inventory_cap_qty_token=s(self.inventory),inventory_available_qty_token=s(max(D(0),self.inventory-self.sold)))
        row['model_history']=history
        risk.update(pause_new=False,force_exit=False,notice_received_at_ms=None,reason_codes=[])
        if self.family=='stale_quotes' and 12<=k<18:m['quotes_received_at_ms']=now-20000
        if self.family=='borrow_recall' and k>=36:
            risk.update(force_exit=True,notice_received_at_ms=BASE+36*H,reason_codes=['MOCK_BORROW_RECALL'])
        return req
    def fill(self,market,side,q,px,k,repair=False):
        fee=q*px*(D('.0006') if market=='spot' else D('.00032'))
        if market=='spot':
            if side=='SELL':
                before=self.sold;self.spot_entry=(self.spot_entry*before+q*px)/(before+q)
                self.sold+=q;self.owned-=q;self.a+=q*px-fee;self.entry_cost+=q*px
            else:
                old=self.sold;self.sold-=q;self.owned+=q;self.a-=q*px+fee
                self.entry_cost*=self.sold/old if old else D(0)
        elif side=='BUY':
            self.perp_entry=(self.perp_entry*self.perp+q*px)/(self.perp+q);self.perp+=q;self.b-=fee
        else:
            self.b+=q*(px-self.perp_entry)-fee;self.perp-=q
        self.fees+=fee
        self.receipts.append({'kind':'fill','market':market,'side':side,'raw_tokens':s(q),
          'exchange_quantity':s(q/(self.mult if market=='perp' else 1)),'price_per_token':s(px),'fee_usdt':s(fee),
          'at_ms':BASE+k*H,'repair':repair,'origin':'synthetic'})
    def execute(self,out,k,request):
        assert out['submission_allowed'] is False
        t=out['targets'][0];self.actions[t['action']]+=1
        self.exit_locked=self.exit_locked or t['exit_locked'];self.episode=t['episode_id']
        w=self.row(k);delta=D(t['delta_pair_qty_token'])
        row=request['instruments'][0];m=row['market'];pf=request['portfolio']
        if delta:
            leg_values=[]
            for leg in t['legs']:
                unit=self.mult if leg['market']=='perp' else D(1)
                if D(leg['delta_quantity_exchange'])*unit!=abs(delta):
                    self.violations.append({'hour':k,'kind':'exchange_multiplier_or_leg_quantity'})
                leg_values.append(D(leg['delta_quantity_exchange'])*D(leg['limit_price_usdt_per_exchange_quantity']))
            budget=max(D(0),D('.03')*min(D(m['spot_quote_volume_capacity_window_usdt']),D(m['perp_quote_volume_capacity_window_usdt']))
                       -D(m['shared_slice_used_usdt'])-D(m['shared_slice_reserved_usdt']))
            if max(leg_values,default=D(0))>budget+D('.00000001'):
                self.violations.append({'hour':k,'kind':'hourly_participation_budget'})
            if delta>0:
                target=D(t['target_pair_qty_token'])
                cap=min(self.inventory,D(m['open_interest_usdt'])*D('.025')/w['perp'],
                        D(m['trailing_24h_paired_quote_volume_usdt'])*D('.05')/w['spot'],D('1000000')/w['spot'])
                if target>cap+D('.00000001'):self.violations.append({'hour':k,'kind':'holding_capacity'})
                room=max(D(0),min(CAPITAL,CAPITAL+D(pf['strategy_marked_pnl_usdt']))-D(pf['used_entry_capital_usdt'])-D(pf['reserved_entry_capital_usdt']))
                if delta*w['spot']>room+D('.00000001'):self.violations.append({'hour':k,'kind':'portfolio_entry_budget'})
            elif any(leg['market']=='perp' and not leg['reduce_only'] for leg in t['legs']):
                self.violations.append({'hour':k,'kind':'perpetual_exit_not_reduce_only'})
        if delta:
            q=quant(abs(delta)*w['fill_fraction'],self.step)
            partial=self.family=='one_leg_delay' and k%9==0 and delta>0
            if q:
                for leg in t['legs']:
                    if partial and leg['market']!=w['partial_leg']:continue
                    px=D(leg['limit_price_usdt_per_exchange_quantity'])/(self.mult if leg['market']=='perp' else 1)
                    self.fill(leg['market'],leg['side'],q,px,k)
                if not partial:self.slices[k]+=q*max(w['spot'],w['perp'])*D('1.0003')
                self.entered=True
        for tr in out['transfer_requests']:
            if not self.pending and tr['from_account']=='B' and tr['to_account']=='A':
                self.pending={'amount':D(tr['amount']),'arrival':k+(3 if self.family=='cash_transfer_delay' else 1)}
        if min(self.a,self.b)<D('-.0000001'):self.violations.append({'hour':k,'kind':'negative_cash'})
        if self.sold>self.inventory or self.perp<0 or self.owned<0:self.violations.append({'hour':k,'kind':'inventory_or_position'})
        if self.exit_locked and t['action'] in ['ENTER','ADD']:self.violations.append({'hour':k,'kind':'exit_lock_reopened'})
        if self.family=='stale_quotes' and 12<=k<18 and t['legs']:self.violations.append({'hour':k,'kind':'stale_quote_executed'})
        self.trace.append({'hour':k,'request_sha256':sha(canon(request).encode()),'decision_sha256':sha(canon(out).encode()),
          'action':t['action'],'exit_locked':t['exit_locked'],'sold_token':s(self.sold),'perp_token':s(self.perp),
          'a_cash':s(self.a),'b_cash':s(self.b),'pending_transfer':self.pending is not None,
          'reasons':t['reason_codes']})
    def finish(self):
        w=self.row(143);unreal=self.perp*(w['perp']-self.perp_entry)
        pnl=self.a+self.b+(self.owned-self.debt)*w['spot']+unreal-CAPITAL
        completed=self.sold==0 and self.perp==0 and self.pending is None
        if completed:
            self.owned-=self.debt;self.receipts.append({'kind':'provider_coin_return','raw_tokens':s(self.debt),'origin':'synthetic'});self.debt=D(0)
        return {'family':self.family,'path':self.index,'entered':self.entered,'complete_simulated_return':completed,
          'remaining_sold_tokens':s(self.sold),'remaining_perp_tokens':s(self.perp),'provider_debt_tokens':s(self.debt),
          'synthetic_marked_pnl_usdt':s(pnl),'completed_annual_return':None,
          'funding_usdt':s(self.funding),'fees_usdt':s(self.fees),'repairs':self.repairs,'settled_transfers':self.transfer_count,
          'actions':dict(self.actions),'violations':self.violations}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--seed');ap.add_argument('--paths-per-family',type=int,default=4);args=ap.parse_args()
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    if (output/'RESULTS.json').exists():raise RuntimeError('Do not overwrite completed mock evidence')
    source_hashes={p.name:sha(p.read_bytes()) for p in [Path(__file__),ROOT/'mock/policy_worker.py',ROOT/'runtime/MODEL_PROFILE.json']}
    seed=args.seed or os.urandom(32).hex();commitment=sha(seed.encode())
    (output/'PROTOCOL.json').write_text(json.dumps({'origin':'synthetic_mock','policy':'frozen_RC2','source_hashes':source_hashes,
      'seed_commitment_sha256':commitment,'families':FAMILIES,'paths_per_family':args.paths_per_family,'hours_per_path':144,
      'policy_process_receives_future':False,'historical_mode':True,'real_orders':False,'real_forecasts':0,
      'return_interpretation':'synthetic execution diagnostics, not realized or forecast investment return'},indent=2)+'\n')
    template=json.loads((ROOT/'runtime/runtime_examples/01_historical_market_enter.input.json').read_text())
    priors=json.loads((ROOT/'runtime/models/r7-model-bundle.json').read_text())['bundle']['asset_priors']
    chosen=[]
    for symbol in ['BICOUSDT','1000PEPEUSDT']:
        chosen.append(dict(priors[symbol],perp_symbol=symbol))
    worker=Worker();results=[];failures=[];counterfactual_checks=0
    try:
        for family in FAMILIES:
            for n in range(args.paths_per_family):
                derived=int(sha((seed+family+str(n)).encode()),16)
                ex=Exchange(derived,family,n,template,chosen[n%2])
                for k in range(-32,0):ex.event(k)
                for k in range(144):
                    ex.event(k)
                    # Let frozen policy observe unmatched legs before the mock
                    # execution team repairs them, preserving RECONCILE evidence.
                    gap_before=ex.sold!=ex.perp
                    req=ex.request(k);reply=worker.ask(req,BASE+k*H)
                    if not reply['ok']:
                        failures.append({'family':family,'path':n,'hour':k,'error':reply['error']});break
                    if k==0:
                        twin=copy.deepcopy(ex)
                        for future in twin.world:
                            if future['k']>0:
                                future['spot']*=2;future['perp']*=D('.5');future['rate']=D('.02')
                        counterfactual=twin.request(0)
                        assert canon(counterfactual)==canon(req),'Future path changed a supposedly observable request'
                        again=worker.ask(counterfactual,BASE+k*H)
                        assert reply==again,'Same observable prefix changed its decision'
                        counterfactual_checks+=1
                    out=reply['decision'];ex.execute(out,k,req)
                    if gap_before:ex.repair(k)
                result=ex.finish();results.append(result)
                folder=output/f'{family}-{n}';folder.mkdir(exist_ok=True)
                (folder/'decisions.jsonl').write_text(''.join(canon(x)+'\n' for x in ex.trace))
                (folder/'receipts.jsonl').write_text(''.join(canon(x)+'\n' for x in ex.receipts))
                print(json.dumps({k:result[k] for k in ['family','path','complete_simulated_return','violations']}),flush=True)
    finally:worker.close()
    (output/'REVEALED_SEED.json').write_text(json.dumps({'seed':seed,'sha256':commitment,'revealed_after_run':True},indent=2)+'\n')
    summary={'origin':'synthetic_mock','paths':len(results),'virtual_hours':sum(sum(r['actions'].values()) for r in results),
       'entered_paths':sum(r['entered'] for r in results),'complete_paths':sum(r['complete_simulated_return'] for r in results),
       'censored_paths':sum(not r['complete_simulated_return'] for r in results),
       'policy_errors':failures,'invariant_violations':[dict(family=r['family'],path=r['path'],**v) for r in results for v in r['violations']],
       'identical_observable_prefix_checks':counterfactual_checks,'submission_allowed':False,'real_execution_verified':False,
       'synthetic_pnl_is_not_a_return_estimate':True,'paths_detail':results}
    (output/'RESULTS.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='paths_detail'},ensure_ascii=False))
    return 1 if failures or summary['invariant_violations'] else 0

if __name__=='__main__':raise SystemExit(main())
