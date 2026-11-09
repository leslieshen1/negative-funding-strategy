"""Offline v2 contract checks. No forecasts, orders, network or ledger mutations."""
import argparse, hashlib, json
from datetime import datetime, timezone
from decimal import Decimal as Q
from pathlib import Path
from schema_check import require, strict_load, shape, KINDS
from units import paired_token_step

ROOT=Path(__file__).resolve().parent
MODEL=strict_load(ROOT/'MODEL_PROFILE.json')
P=MODEL['profile']; ZERO=Q(0); EPS=Q('0.000000000000001'); HOUR=3600000

def close(a,b,why):require(abs(a-b)<=EPS,why)
def month_start_ms(now):
    d=datetime.fromtimestamp(now/1000,tz=timezone.utc)
    return int(d.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
def complete_quotes(m):
    return all(m[x] is not None and Q(m[x])>0 for x in ['spot_bid_usdt_per_token','spot_ask_usdt_per_token',
        'perp_bid_usdt_per_token','perp_ask_usdt_per_token'])
def slice_room(m):
    raw=min(Q(m['spot_quote_volume_capacity_window_usdt'] or '0'),Q(m['perp_quote_volume_capacity_window_usdt'] or '0'))*Q(P['pov'])
    room=max(ZERO,raw-Q(m['shared_slice_used_usdt'])-Q(m['shared_slice_reserved_usdt']))
    return min(room,Q(m['shared_slice_budget_remaining_usdt'])) if 'shared_slice_budget_remaining_usdt' in m else room
def needs_exit(req,row):
    pos,m=row['position'],row['market']
    held=Q(pos['strategy_sold_spot_qty_token'])>0 or Q(pos['perp_long_qty_token'])>0 or Q(pos['episode_coin_return_due_token'])>0
    return pos['exit_locked'] or (held and (req['portfolio']['risk_state']=='EXIT_ALL' or row['risk']['force_exit'] or
        (m['settlement_history_complete'] and m['raw_settled_sum_24h'] is not None and Q(m['raw_settled_sum_24h'])>0)))

def validate(v):
    require(v.get('message_type') in KINDS,'unknown message_type')
    canonical=json.dumps(P,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    require(hashlib.sha256(canonical).hexdigest()==MODEL['profile_sha256'],'local profile hash mismatch')
    shape(v,strict_load(ROOT/'schemas'/(KINDS[v['message_type']]+'.schema.json')))
    now=v['as_of_ms'];kind=v['message_type']
    if kind=='strategy_input':
        pf=v['portfolio'];require(pf['snapshot_received_at_ms']<=now,'future account snapshot')
        free=max(ZERO,min(Q(pf['account_b_wallet_usdt'])-Q(pf['account_b_initial_margin_1x_usdt']),Q(pf['account_b_equity_usdt'])-Q(pf['account_b_position_notional_usdt'])))
        require(Q(pf['account_b_free_usdt'])<=free,'B free cash includes reserved margin or ignores equity loss')
        require(Q(pf['account_b_transferable_usdt'])<=Q(pf['account_b_free_usdt']),'B transfer exceeds available cash')
        seen=set();symbols=set();tokens=set()
        for row in v['instruments']:
            i,pos,m,li=row['instrument'],row['position'],row['market'],row['limits']
            require(i['instrument_id'] not in seen,'duplicate instrument');seen.add(i['instrument_id'])
            require(i['perp_symbol'] not in symbols and i['token'] not in tokens,'duplicate symbol or shared token inventory')
            symbols.add(i['perp_symbol']);tokens.add(i['token'])
            paired_token_step(i['spot_qty_step_token'],i['perp_qty_step_exchange'],i['tokens_per_perp_quantity'])
            require(Q(i['tokens_per_perp_quantity'])>0,'nonpositive multiplier')
            close(Q(pos['perp_long_qty_exchange'])*Q(i['tokens_per_perp_quantity']),Q(pos['perp_long_qty_token']),'input perpetual unit mismatch')
            for key in ['observed_at_ms','previous_bar_end_ms','capacity_window_end_ms','latest_settlement_time_ms',
                'latest_settlement_received_at_ms','quotes_received_at_ms','completed_inputs_received_at_ms']:
                require(m[key]<=now,'future input:'+key)
            require(m['latest_settlement_time_ms']<=m['latest_settlement_received_at_ms'],'settlement received before event')
            require(m['capacity_window_start_ms']+HOUR==m['capacity_window_end_ms'],'capacity must cover one completed hour')
            require(m['capacity_window_end_ms']==m['previous_bar_end_ms'] and m['previous_bar_end_ms']%HOUR==0,'capacity/bar boundary mismatch')
            require(m['capacity_window_end_ms']<=m['completed_inputs_received_at_ms'],'bar received before complete')
            require(m['settlement_coverage_start_ms']<=now-24*HOUR,'24h history coverage too short')
            if m['latest_raw_rate'] is not None and m['latest_interval_hours'] is not None:
                require(Q(m['latest_interval_hours'])>0,'invalid funding interval')
                if m['settled_r8'] is not None:close(Q(m['settled_r8']),Q(m['latest_raw_rate'])*8/Q(m['latest_interval_hours']),'funding normalization mismatch')
            stamps=set();eligible=[]
            for f in m['settlements']:
                require(f['funding_time_ms']<=f['received_at_ms']<=now,'future funding history')
                require(Q(f['interval_hours'])>0,'invalid event interval')
                require(f['funding_time_ms'] not in stamps,'duplicate funding settlement');stamps.add(f['funding_time_ms'])
                if now-24*HOUR<f['funding_time_ms']<=now:eligible.append(f)
            if m['settlement_history_complete']:
                require(m['raw_settled_sum_24h'] is not None,'complete history missing sum')
                close(sum((Q(x['raw_rate']) for x in eligible),ZERO),Q(m['raw_settled_sum_24h']),'raw 24h sum mismatch')
            if m['settlements']:
                last=max(m['settlements'],key=lambda x:x['funding_time_ms'])
                require(last['funding_time_ms']==m['latest_settlement_time_ms'] and last['received_at_ms']==m['latest_settlement_received_at_ms'],'latest settlement is not latest economic event')
                require(m['latest_raw_rate'] is not None and m['latest_interval_hours'] is not None,'latest event missing rate/interval')
                close(Q(last['raw_rate']),Q(m['latest_raw_rate']),'latest raw rate mismatch')
                close(Q(last['interval_hours']),Q(m['latest_interval_hours']),'latest interval mismatch')
            elif m['settlement_history_complete'] and m['latest_settlement_time_ms']>now-24*HOUR:
                require(False,'complete history omitted latest settlement')
            if complete_quotes(m):
                require(Q(m['spot_bid_usdt_per_token'])<=Q(m['spot_ask_usdt_per_token']) and Q(m['perp_bid_usdt_per_token'])<=Q(m['perp_ask_usdt_per_token']),'crossed book')
            if m['open_interest_usdt'] is not None and 'max_position_notional_from_oi_usdt' in li:
                close(Q(li['max_position_notional_from_oi_usdt']),Q(m['open_interest_usdt'])*Q(P['oi_cap_fraction']),'OI cap/profile mismatch')
            if m['trailing_24h_paired_quote_volume_usdt'] is not None and 'max_position_notional_from_volume_usdt' in li:
                close(Q(li['max_position_notional_from_volume_usdt']),Q(m['trailing_24h_paired_quote_volume_usdt'])*Q(P['trailing_24h_volume_cap_fraction']),'volume cap/profile mismatch')
            if 'per_coin_cap_usdt' in li:close(Q(li['per_coin_cap_usdt']),Q(P['per_coin_entry_cap_usdt']),'single coin cap/profile mismatch')
            raw=min(Q(m['spot_quote_volume_capacity_window_usdt'] or '0'),Q(m['perp_quote_volume_capacity_window_usdt'] or '0'))*Q(P['pov'])
            room=max(ZERO,raw-Q(m['shared_slice_used_usdt'])-Q(m['shared_slice_reserved_usdt']))
            if 'shared_slice_budget_remaining_usdt' in m:require(Q(m['shared_slice_budget_remaining_usdt'])<=room,'shared participation budget inflated')
            n=row['risk']['notice_received_at_ms']
            require(n is None or n<=now,'future risk notice')
            if row['risk']['force_exit']:require(n is not None,'forced exit lacks notice time')
    elif kind=='strategy_output':
        require(now<v['valid_until_ms']<=now+P['decision_ttl_ms'],'invalid decision expiry')
        require(v['submission_allowed'] is False,'integration candidate cannot authorize submission')
        require(not(v['status']=='REJECT' and v['targets']),'rejected output has targets')
        seen=set()
        for t in v['targets']:
            require(t['instrument_id'] not in seen,'duplicate target');seen.add(t['instrument_id'])
            cur,tgt,delta,goal=map(Q,[t['current_pair_qty_token'],t['target_pair_qty_token'],t['delta_pair_qty_token'],t['desired_pair_qty_token']])
            close(tgt-cur,delta,'target delta mismatch')
            mult=Q(t['tokens_per_perp_quantity']);require(mult>0,'invalid multiplier')
            close(Q(t['target_perp_qty_exchange'])*mult,tgt,'output perpetual unit mismatch')
            act=t['action'];d=t['model_diagnostics']
            require(d['observed_at_ms']<=now,'income model uses unavailable outcomes')
            cutoff=d.get('income_model_valid_from_ms')
            if cutoff is None:cutoff=month_start_ms(now)
            anchor=P['income_initial_anchor_ms']
            require(cutoff==month_start_ms(now) or (cutoff==anchor and anchor<=now<1761955200000),'income model refresh is not allowed by schedule')
            if d['status']=='VALID':
                require(d['income_training_outcomes_end_ms'] is not None and d['income_training_outcomes_end_ms']<cutoff,'income model uses unavailable outcomes')
                if d.get('income_model_valid_until_ms') is not None:require(cutoff<=now<d['income_model_valid_until_ms'],'income model expired')
            gate=d['status']=='VALID' and d['expected_income_rate'] is not None and d['round_trip_cost_rate'] is not None and Q(d['expected_income_rate'])>=Q(P['income_cost_multiple'])*Q(d['round_trip_cost_rate'])
            require(d['r7_gate_passed']==gate,'income gate mismatch')
            if act in ['ENTER','ADD']:
                require(delta>0 and goal>=tgt and not t['exit_locked'],'invalid entry/add')
                require((cur==0)==(act=='ENTER'),'wrong ENTER/ADD state')
            elif act=='EXIT':require(goal==0 and delta<=0 and t['exit_locked'],'invalid EXIT')
            elif act=='REDUCE':require(goal<=tgt<cur and delta<0,'invalid REDUCE')
            else:require(delta==0 and not t['legs'],'hold/reconcile has orders')
            if t['exit_locked']:require(t['risk_state'] in ['EXIT_LOCKED','RECONCILE'] and act not in ['ENTER','ADD'],'exit lock state mismatch')
            require((delta!=0)==bool(t['legs']),'executable delta/legs mismatch')
            if t['legs']:
                require(len(t['legs'])==2 and {(x['account'],x['market']) for x in t['legs']}=={('A','spot'),('B','perp')},'paired legs required')
                for leg in t['legs']:
                    ratio=Q(1) if leg['market']=='spot' else mult
                    close(Q(leg['delta_quantity_exchange'])*ratio,abs(delta),'leg unit mismatch')
                    close(Q(leg['equivalent_raw_tokens']),abs(delta),'leg token mismatch')
                    side=('SELL' if delta>0 else 'BUY') if leg['market']=='spot' else ('BUY' if delta>0 else 'SELL')
                    require(leg['side']==side,'leg side mismatch')
                    require(leg['reduce_only']==(leg['market']=='perp' and delta<0),'reduce-only mismatch')
                    require(leg['quantity_unit']==('raw_token' if leg['market']=='spot' else 'perp_exchange_quantity'),'wrong quantity unit label')
                    require(Q(leg['limit_price_usdt_per_exchange_quantity'])>0,'invalid execution price bound')
    else:
        require(v['ledger_version_after']>v['ledger_version_before'],'ledger version must advance')
        seen=set()
        for f in v['fills']:
            ident=(f['account'],f['market'],f['symbol'],f['trade_id'])
            require(ident not in seen,'duplicate fill');seen.add(ident)
            require(Q(f['quantity_exchange'])>0 and Q(f['tokens_per_exchange_quantity'])>0 and Q(f['price_usdt_per_exchange_quantity'])>0,'invalid fill quantity/price')
            close(Q(f['quantity_exchange'])*Q(f['tokens_per_exchange_quantity']),Q(f['quantity_token']),'fill unit mismatch')
            require((f['account'],f['market']) in [('A','spot'),('B','perp')],'fill account/market mismatch')
            require(f['event_time_ms']<=f['received_at_ms']<=now,'future fill')
        for p in v['positions_after']:
            close(Q(p['perp_long_qty_exchange'])*Q(p['instrument']['tokens_per_perp_quantity']),Q(p['perp_long_qty_token']),'receipt position units')
            close(Q(p['perp_long_qty_token'])-Q(p['strategy_sold_spot_qty_token']),Q(p['hedge_error_qty_token']),'receipt hedge error')
        for array,key in [('transfer_receipts','request_id'),('funding_receipts','event_id'),('coin_return_receipts','receipt_id')]:
            ids=set()
            for x in v[array]:
                require(x[key] not in ids,'duplicate '+array);ids.add(x[key])
                if array=='transfer_receipts':
                    require(x['from_account']!=x['to_account'] and Q(x['amount'])>0,'invalid transfer')
                    require((x['settled_at_ms'] is not None)==(x['status']=='SETTLED'),'transfer settlement status/time mismatch')
                    if x['settled_at_ms'] is not None:require(x['settled_at_ms']<=now,'future transfer')
                else:
                    event=x['funding_time_ms'] if array=='funding_receipts' else x['event_time_ms']
                    require(event<=x['received_at_ms']<=now,'future receipt')
                    if array=='funding_receipts':require(Q(x['position_qty_exchange'])>=0 and Q(x['tokens_per_exchange_quantity'])>0,'invalid funding position')
                    else:require(Q(x['quantity_token'])>0,'invalid coin return')
    return True

def validate_pair(req,res,now_ms):
    validate(req);validate(res)
    require(req['message_type']=='strategy_input' and res['message_type']=='strategy_output','wrong pair')
    require(req['request_id']==res['input_request_id'],'request ID mismatch')
    for key in ['model','ledger_version','mode','as_of_ms']:require(req[key]==res[key],key+' mismatch')
    require(res['as_of_ms']<=now_ms<res['valid_until_ms'],'expired/not yet valid')
    rows={r['instrument']['instrument_id']:r for r in req['instruments']}
    if res['status']!='REJECT':require({t['instrument_id'] for t in res['targets']}==set(rows),'response omitted instrument state')
    pf=req['portfolio'];entry_total=ZERO;b_need=ZERO;a_need=ZERO
    require(Q(pf['used_entry_capital_usdt'])>=sum((Q(r['position']['entry_cost_usdt']) for r in rows.values()),ZERO),'portfolio cost below supplied positions')
    require(Q(pf['reserved_entry_capital_usdt'])>=sum((Q(r['position']['reserved_entry_cost_usdt']) for r in rows.values()),ZERO),'portfolio reservations below instrument reservations')
    for t in res['targets']:
        require(t['instrument_id'] in rows,'unknown instrument')
        row=rows[t['instrument_id']];i,pos,m,li=row['instrument'],row['position'],row['market'],row['limits']
        act=t['action'];d=t['model_diagnostics'];cur,tgt,delta=map(Q,[t['current_pair_qty_token'],t['target_pair_qty_token'],t['delta_pair_qty_token']])
        require(t['tokens_per_perp_quantity']==i['tokens_per_perp_quantity'],'instrument multiplier drift')
        if needs_exit(req,row):
            require(t['exit_locked'],'exit lock cancelled or omitted')
            require(Q(t['desired_pair_qty_token'])==0 or act=='RECONCILE','exit lock must retain zero final goal')
        unmatched=Q(pos['strategy_sold_spot_qty_token'])!=Q(pos['perp_long_qty_token'])
        if 'hedge_error_qty_token' in t:close(Q(t['hedge_error_qty_token']),Q(pos['perp_long_qty_token'])-Q(pos['strategy_sold_spot_qty_token']),'hedge error output mismatch')
        if unmatched or pf['risk_state']=='RECONCILE':require(act=='RECONCILE','unmatched/uncertain state requires reconciliation')
        else:close(cur,Q(pos['strategy_sold_spot_qty_token']),'stale current position')
        if delta!=0:
            require(m['status']=='OK' and complete_quotes(m),'orders require current quotes')
            require(0<=now_ms-m['quotes_received_at_ms']<=P['max_input_age_ms'],'stale quote')
            require(0<=now_ms-pf['snapshot_received_at_ms']<=P['max_input_age_ms'],'stale accounts')
            require(m['capacity_window_complete'],'incomplete capacity window')
            require(m['capacity_window_end_ms']==req['as_of_ms']//HOUR*HOUR,'stale capacity hour')
            qstep=paired_token_step(i['spot_qty_step_token'],i['perp_qty_step_exchange'],i['tokens_per_perp_quantity'])
            require(abs(delta)%qstep==0,'paired lot quantum violation')
            bounded={leg['market']:Q(leg['limit_price_usdt_per_exchange_quantity'])/(1 if leg['market']=='spot' else Q(i['tokens_per_perp_quantity'])) for leg in t['legs']}
            notional=abs(delta)*max(*bounded.values(),Q(m['spot_bid_usdt_per_token']),Q(m['perp_bid_usdt_per_token']))
            require(notional<=Q(t['max_slice_notional_usdt'])<=slice_room(m),'shared participation exceeded')
            require(abs(delta)>=Q(i['spot_min_qty_token']) and abs(delta)/Q(i['tokens_per_perp_quantity'])>=Q(i['perp_min_qty_exchange']),'minimum order quantity')
            require(abs(delta)*Q(m['spot_bid_usdt_per_token'])>=Q(i['spot_min_notional_usdt']) and abs(delta)*Q(m['perp_bid_usdt_per_token'])>=Q(i['perp_min_notional_usdt']),'minimum order notional')
            for leg in t['legs']:require(leg['symbol']==i['spot_symbol' if leg['market']=='spot' else 'perp_symbol'],'leg symbol mismatch')
            if delta<0:a_need+=abs(delta)*bounded['spot']*(1+Q(P['spot_taker_fee']))
        if act in ['ENTER','ADD']:
            require(pf['risk_state']=='NORMAL' and not row['risk']['pause_new'] and not row['risk']['force_exit'],'risk pause/exit prohibits adds')
            require(m['settlement_history_complete'] and m['settled_r8_source']=='latest_settled' and m['settled_r8'] is not None,'entry requires complete settled signal')
            require(m['settlements'] and m['latest_raw_rate'] is not None and m['latest_interval_hours'] is not None and m['latest_settlement_time_ms']>req['as_of_ms']-24*HOUR,'incomplete/stale settled signal')
            require(Q(m['open_interest_usdt'] or '0')>0 and Q(m['trailing_24h_paired_quote_volume_usdt'] or '0')>0,'unknown capacity cannot open')
            require(m['spot_close_usdt_per_token'] is not None and m['perp_close_usdt_per_token'] is not None,'missing prior prices')
            S,F=Q(m['spot_close_usdt_per_token']),Q(m['perp_close_usdt_per_token'])
            require(S>0 and F>0,'nonpositive prior price')
            rate=Q(m['settled_r8'])
            if cur>0 and rate<0:rate=min(ZERO,rate+Q(P['own_funding_impact_k'])*Q('.01')*cur*F/Q(m['open_interest_usdt']))
            require(d['effective_entry_r8'] is not None,'missing effective rate')
            close(Q(d['effective_entry_r8']),rate,'own-impact entry rate mismatch')
            require(rate<Q(P['entry_rate_r8_strict_below']),'strict funding threshold failed')
            require((F-S)/S<=Q(P['entry_basis_max']),'basis threshold failed')
            require(d['r7_gate_passed'] and not d['adl_entry_blocked'],'income/ADL entry gate failed')
            require(d['income_sample_count']>=200,'income model has too few completed samples')
            require(Q(d['round_trip_cost_rate'])>=Q(P['income_gate_round_trip_fee_allowance']),'roundtrip allowance missing')
            reserved=Q(pos['reserved_add_qty_token'])
            require(tgt+reserved<=Q(li['inventory_cap_qty_token']) and delta<=Q(li['inventory_available_qty_token']),'inventory cap exceeded')
            require(Q(pos['entry_cost_usdt'])+Q(pos['reserved_entry_cost_usdt'])+delta*S<=Q(P['per_coin_entry_cap_usdt']),'single coin entry cost exceeded')
            require(tgt+reserved<=min(Q(m['open_interest_usdt'])*Q(P['oi_cap_fraction'])/F,Q(m['trailing_24h_paired_quote_volume_usdt'])*Q(P['trailing_24h_volume_cap_fraction'])/S,Q(P['per_coin_mark_target_usdt'])/S),'position capacity exceeded')
            entry_total+=delta*S
            b_need+=delta*bounded['perp']*(1+Q(P['perp_taker_fee']))
    budget=max(ZERO,min(Q(P['capital_usdt']),Q(P['capital_usdt'])+Q(pf['strategy_marked_pnl_usdt']))-Q(pf['used_entry_capital_usdt'])-Q(pf['reserved_entry_capital_usdt']))
    require(entry_total<=budget,'shared portfolio budget exceeded')
    require(b_need<=Q(pf['account_b_free_usdt']),'insufficient settled B margin; pending transfers do not count')
    require(a_need<=Q(pf['account_a_free_usdt']),'insufficient settled A buyback cash; pending transfers do not count')
    out={'A':ZERO,'B':ZERO}
    for tr in res['transfer_requests']:
        require(pf['internal_transfers_allowed'] and tr['from_account']!=tr['to_account'] and Q(tr['amount'])>0,'invalid transfer request')
        require(tr['required_before_order_submission'],'transfer requires a fresh settled snapshot')
        out[tr['from_account']]+=Q(tr['amount'])
    require(out['A']+a_need<=Q(pf['account_a_free_usdt']),'A cash double allocation')
    require(out['B']<=Q(pf['account_b_transferable_usdt']) and out['B']+b_need<=Q(pf['account_b_free_usdt']),'B cash double allocation')
    return True

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('message');ap.add_argument('--input');ap.add_argument('--now-ms',type=int);args=ap.parse_args()
    try:
        v=strict_load(args.message)
        if args.input:
            require(args.now_ms is not None,'paired validation requires receiver/replay clock')
            validate_pair(strict_load(args.input),v,args.now_ms)
        else:validate(v)
        print('PASS')
    except (ValueError,KeyError,ArithmeticError) as exc:print('REJECT:',exc);raise SystemExit(2)
