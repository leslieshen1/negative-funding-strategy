"""Generate a constrained shadow decision from raw state, r7 model and history."""
from decimal import Decimal as Q,ROUND_FLOOR
from pathlib import Path
import argparse,hashlib,json,sys,time
from online_scorer import Scorer
from validate import validate,validate_pair,strict_load,require,complete_quotes,slice_room,needs_exit,P,MODEL
from units import paired_token_step

ZERO=Q(0);ONE=Q(1);HOUR=3600000
def s(x):return format(Q(x),'f')
def floor(q,step):return max(ZERO,(q/step).to_integral_value(rounding=ROUND_FLOOR)*step)
def live_quotes(row,pf,now):
    m=row['market']
    return (m['status']=='OK' and complete_quotes(m) and 0<=now-m['quotes_received_at_ms']<=P['max_input_age_ms']
        and 0<=now-pf['snapshot_received_at_ms']<=P['max_input_age_ms']
        and m['capacity_window_complete'] and m['capacity_window_end_ms']==now//HOUR*HOUR)
def capacity(row):
    m,li=row['market'],row['limits']
    if any(m[k] is None for k in ['open_interest_usdt','trailing_24h_paired_quote_volume_usdt','spot_close_usdt_per_token','perp_close_usdt_per_token']):return None
    S,F=Q(m['spot_close_usdt_per_token']),Q(m['perp_close_usdt_per_token'])
    if S<=0 or F<=0:return None
    return max(ZERO,min(Q(li['inventory_cap_qty_token']),Q(m['open_interest_usdt'])*Q(P['oi_cap_fraction'])/F,
        Q(m['trailing_24h_paired_quote_volume_usdt'])*Q(P['trailing_24h_volume_cap_fraction'])/S,
        Q(P['per_coin_mark_target_usdt'])/S))
def legs(row,delta):
    if not delta:return []
    i,m=row['instrument'],row['market'];mult=Q(i['tokens_per_perp_quantity']);out=[]
    for market,account in [('spot','A'),('perp','B')]:
        side=('SELL' if delta>0 else 'BUY') if market=='spot' else ('BUY' if delta>0 else 'SELL')
        ratio=ONE if market=='spot' else mult
        px=Q(m[market+('_ask' if side=='BUY' else '_bid')+'_usdt_per_token'])*ratio
        out.append({'account':account,'market':market,'symbol':i['spot_symbol' if market=='spot' else 'perp_symbol'],
            'side':side,'delta_quantity_exchange':s(abs(delta)/ratio),
            'quantity_unit':'raw_token' if market=='spot' else 'perp_exchange_quantity',
            'equivalent_raw_tokens':s(abs(delta)),'reduce_only':market=='perp' and delta<0,
            'limit_price_usdt_per_exchange_quantity':s(px)})
    return out
def minima(row,qty):
    if qty<=0:return False
    i,m=row['instrument'],row['market']
    return (qty>=Q(i['spot_min_qty_token']) and qty/Q(i['tokens_per_perp_quantity'])>=Q(i['perp_min_qty_exchange'])
        and qty*Q(m['spot_bid_usdt_per_token'])>=Q(i['spot_min_notional_usdt'])
        and qty*Q(m['perp_bid_usdt_per_token'])>=Q(i['perp_min_notional_usdt']))

def decide(request,scorer,now_ms=None,replay=False):
    validate(request);require(request['message_type']=='strategy_input','not a strategy input')
    require(scorer.sha==P['r7_model_bundle_sha256'],'model bundle is not pinned by this strategy profile')
    now=request['as_of_ms'];clock=int(time.time()*1000) if now_ms is None else now_ms
    require(now<=clock<now+P['decision_ttl_ms'],'STALE_OR_FUTURE_REQUEST')
    require(not(replay and request['mode']=='live'),'replay flag cannot be used with live mode')
    pf=request['portfolio'];identity={k:MODEL[k] for k in ['profile_sha256','engine_source_sha256']};identity['strategy_id']=P['strategy_id']
    digest=hashlib.sha256((json.dumps(request,sort_keys=True,separators=(',',':'))+scorer.sha+str(replay)).encode()).hexdigest()
    out={'protocol_version':'2.1.0','message_type':'strategy_output','decision_id':'dec-'+digest[:32],
        'input_request_id':request['request_id'],'as_of_ms':now,'valid_until_ms':now+P['decision_ttl_ms'],
        'ledger_version':request['ledger_version'],'mode':request['mode'],'model':identity,
        'status':'HOLD','submission_allowed':False,'reason_codes':['SHADOW_DECISION_ONLY'],
        'targets':[],'transfer_requests':[]}
    portfolio_room=max(ZERO,min(Q(P['capital_usdt']),Q(P['capital_usdt'])+Q(pf['strategy_marked_pnl_usdt']))
        -Q(pf['used_entry_capital_usdt'])-Q(pf['reserved_entry_capital_usdt']))
    a_cash=Q(pf['account_a_free_usdt']);b_cash=Q(pf['account_b_free_usdt']);b_transfer=Q(pf['account_b_transferable_usdt'])
    margin_shortfall=max(ZERO,Q(pf['account_b_initial_margin_1x_usdt'])-Q(pf['account_b_wallet_usdt']),
                         Q(pf['account_b_position_notional_usdt'])-Q(pf['account_b_equity_usdt']))
    if margin_shortfall>0:out['reason_codes'].append('ONE_X_MARGIN_SHORTFALL_REQUIRES_REPAIR')
    states=[];candidates=[]
    for row in request['instruments']:
        i,pos,m=row['instrument'],row['position'],row['market'];sold=Q(pos['strategy_sold_spot_qty_token']);perp=Q(pos['perp_long_qty_token'])
        current=min(sold,perp);locked=needs_exit(request,row)
        diag,reasons=scorer.score(row,now,replay=replay)
        t={'instrument_id':i['instrument_id'],'episode_id':pos['episode_id'],'action':'HOLD',
            'current_pair_qty_token':s(current),'desired_pair_qty_token':s(current),'target_pair_qty_token':s(current),
            'delta_pair_qty_token':'0','target_perp_qty_exchange':s(current/Q(i['tokens_per_perp_quantity'])),
            'tokens_per_perp_quantity':i['tokens_per_perp_quantity'],'exit_locked':locked,'max_slice_notional_usdt':'0',
            'reason_codes':list(reasons),'legs':[],'model_diagnostics':diag,
            'risk_state':'EXIT_LOCKED' if locked else 'PAUSE_NEW' if pf['risk_state']!='NORMAL' or row['risk']['pause_new'] else 'NORMAL',
            'hedge_error_qty_token':s(perp-sold)}
        if locked:t.update(action='EXIT',desired_pair_qty_token='0')
        if sold!=perp or pf['risk_state']=='RECONCILE':
            t.update(action='RECONCILE',risk_state='RECONCILE');t['reason_codes'].append('ACTUAL_LEGS_OR_LEDGER_REQUIRE_RECONCILIATION')
            if locked:t['desired_pair_qty_token']='0'
            states.append((row,t));continue
        cap=capacity(row);goal=ZERO if locked else min(current,cap) if cap is not None else current
        if locked or goal<current:
            t['desired_pair_qty_token']=s(goal);t['reason_codes'].append('EXIT_LOCKED' if locked else 'CAPACITY_RISK_TRIM')
            if not live_quotes(row,pf,clock):t['reason_codes'].append('WAIT_FRESH_QUOTES_ACCOUNTS_OR_CAPACITY')
            else:
                step=paired_token_step(i['spot_qty_step_token'],i['perp_qty_step_exchange'],i['tokens_per_perp_quantity'])
                price=max(Q(m['spot_ask_usdt_per_token']),Q(m['perp_bid_usdt_per_token']))
                needed=current-goal;cash_unit=Q(m['spot_ask_usdt_per_token'])*(1+Q(P['spot_taker_fee']));a_before=a_cash
                amount=floor(min(needed,slice_room(m)/price,a_cash/cash_unit),step)
                if minima(row,amount):
                    t.update(action='EXIT' if locked else 'REDUCE',target_pair_qty_token=s(current-amount),delta_pair_qty_token=s(-amount),
                        target_perp_qty_exchange=s((current-amount)/Q(i['tokens_per_perp_quantity'])),legs=legs(row,-amount),max_slice_notional_usdt=s(amount*price))
                    a_cash-=amount*cash_unit
                else:t['reason_codes'].append('UNFILLED_DUE_TO_CASH_CAPACITY_OR_MINIMUM_ORDER')
                shortage=max(ZERO,needed*cash_unit-a_before)
                if shortage>0:
                    t['reason_codes'].append('WAIT_SETTLED_A_BUYBACK_CASH')
                    if Q(pf['unsettled_transfer_usdt'])>0:t['reason_codes'].append('TRANSFER_ALREADY_PENDING')
                    elif pf['internal_transfers_allowed']:
                        transfer=min(shortage,b_transfer,b_cash)
                        if transfer>0:
                            out['transfer_requests'].append({'request_id':'tr-'+digest[:16]+'-'+str(len(out['transfer_requests'])),
                                'from_account':'B','to_account':'A','asset':'USDT','amount':s(transfer),'required_before_order_submission':True})
                            b_cash-=transfer;b_transfer-=transfer
            states.append((row,t));continue
        if margin_shortfall>0:
            t['risk_state']='PAUSE_NEW';t['reason_codes'].append('ONE_X_MARGIN_SHORTFALL_REQUIRES_REPAIR')
        elif locked or row['risk']['force_exit'] or pf['risk_state']!='NORMAL' or row['risk']['pause_new']:
            t['reason_codes'].append('RISK_PROHIBITS_NEW_EXPOSURE')
        elif diag['status']!='VALID':t['reason_codes'].append('MODEL_UNAVAILABLE_NO_NEW_EXPOSURE')
        elif not live_quotes(row,pf,clock):t['reason_codes'].append('WAIT_FRESH_QUOTES_ACCOUNTS_OR_CAPACITY')
        elif not m['settlement_history_complete'] or m['settled_r8_source']!='latest_settled' or not m['settlements'] or m['latest_settlement_time_ms']<=now-24*HOUR:
            t['reason_codes'].append('COMPLETE_SETTLED_SIGNAL_REQUIRED')
        elif diag['effective_entry_r8'] is None or Q(diag['effective_entry_r8'])>=Q(P['entry_rate_r8_strict_below']):t['reason_codes'].append('RATE_NOT_STRICTLY_BELOW_THRESHOLD')
        elif (Q(m['perp_close_usdt_per_token'])-Q(m['spot_close_usdt_per_token']))/Q(m['spot_close_usdt_per_token'])>Q(P['entry_basis_max']):t['reason_codes'].append('BASIS_NOT_QUALIFIED')
        elif diag['adl_entry_blocked']:t['reason_codes'].append('ADL_PUBLIC_PROXY_BLOCK')
        elif not diag['r7_gate_passed']:t['reason_codes'].append('EXPECTED_INCOME_DOES_NOT_COVER_DOUBLE_COST')
        elif cap is None:t['reason_codes'].append('CAPACITY_UNKNOWN')
        else:candidates.append((Q(diag['effective_entry_r8']),len(states),row,t,cap))
        states.append((row,t))
    # Only remaining settled A cash can be requested for collateral repair.
    # Neither the intention nor an existing pending transfer increases B cash.
    if margin_shortfall>0:
        if Q(pf['unsettled_transfer_usdt'])>0:out['reason_codes'].append('MARGIN_TRANSFER_PENDING_CONFIRM_ACTUAL_ARRIVAL')
        elif pf['internal_transfers_allowed'] and a_cash>0:
            transfer=min(margin_shortfall,a_cash)
            out['transfer_requests'].append({'request_id':'tr-'+digest[:16]+'-margin',
                'from_account':'A','to_account':'B','asset':'USDT','amount':s(transfer),'required_before_order_submission':True})
            a_cash-=transfer
            if transfer<margin_shortfall:out['reason_codes'].append('MARGIN_REPAIR_STILL_UNFUNDED')
        else:out['reason_codes'].append('MARGIN_REPAIR_UNFUNDED_EXECUTOR_RISK_INTERVENTION_REQUIRED')
    # Freeze original ordering: deeper effective negative rates first. Exits
    # have consumed cash/intents first; never credit a projected sale/transfer.
    for _,_,row,t,cap in sorted(candidates,key=lambda item:(item[0],item[1])):
        i,pos,m=row['instrument'],row['position'],row['market'];current=Q(t['current_pair_qty_token']);S=Q(m['spot_close_usdt_per_token'])
        step=paired_token_step(i['spot_qty_step_token'],i['perp_qty_step_exchange'],i['tokens_per_perp_quantity'])
        reserved=Q(pos['reserved_add_qty_token']);cost_room=max(ZERO,Q(P['per_coin_entry_cap_usdt'])-Q(pos['entry_cost_usdt'])-Q(pos['reserved_entry_cost_usdt']))
        desired_room=max(ZERO,min(cap-current-reserved,Q(row['limits']['inventory_available_qty_token']),cost_room/S,portfolio_room/S))
        desired=current+floor(desired_room,step)
        price=max(Q(m['spot_bid_usdt_per_token']),Q(m['perp_ask_usdt_per_token']))
        collateral=Q(m['perp_ask_usdt_per_token'])*(1+Q(P['perp_taker_fee']))
        amount=floor(min(desired_room,slice_room(m)/price,b_cash/collateral),step)
        if not minima(row,amount):t['reason_codes'].append('NO_SPENDABLE_INVENTORY_MARGIN_CAPACITY_OR_LOT');continue
        t.update(action='ENTER' if current==0 else 'ADD',desired_pair_qty_token=s(desired),target_pair_qty_token=s(current+amount),
            delta_pair_qty_token=s(amount),target_perp_qty_exchange=s((current+amount)/Q(i['tokens_per_perp_quantity'])),
            max_slice_notional_usdt=s(amount*price),legs=legs(row,amount))
        t['reason_codes'].append('FROZEN_R7_AND_ENTRY_GATES_PASS')
        if t['episode_id'] is None:t['episode_id']='ep-'+digest[:16]+'-'+str(len(out['targets']))+'-'+i['instrument_id']
        portfolio_room-=amount*S;b_cash-=amount*collateral
    out['targets']=[t for _,t in states]
    if any(t['legs'] for t in out['targets']):out['status']='OK'
    validate_pair(request,out,clock)
    return out

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--input',required=True)
    ap.add_argument('--model',default=str(Path(__file__).parent/'models/r7-model-bundle.json'));ap.add_argument('--output');ap.add_argument('--replay',action='store_true')
    args=ap.parse_args()
    try:
        request=strict_load(args.input);scorer=Scorer.from_path(args.model)
        result=decide(request,scorer,now_ms=request['as_of_ms'] if args.replay else None,replay=args.replay)
        text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        if args.output:Path(args.output).write_text(text)
        else:sys.stdout.write(text)
    except (ValueError,KeyError,ArithmeticError) as exc:
        print(json.dumps({'status':'REJECT','error':str(exc)},ensure_ascii=False),file=sys.stderr);raise SystemExit(2)

if __name__=='__main__':main()
