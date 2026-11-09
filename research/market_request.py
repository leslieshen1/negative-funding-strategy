"""Historical public-market fixtures with explicitly synthetic accounts/filters."""
from decimal import Decimal as Q
from pathlib import Path
import copy,json,math

HOUR=3600000
def value(x):return format(Q(str(float(x))),'f') if math.isfinite(float(x)) else None
def model_row(D,ivh,t,n,start_ms):
    history=[]
    for u in range(t-26,t):
        end=start_ms+(u+1)*HOUR
        history.append({'bar_end_ms':end,'received_at_ms':end,'spot_close_usdt_per_token':value(D.arr[1][u,n]),
            'perp_close_usdt_per_token':value(D.arr[4][u,n]),'oi_usdt':value(D.oih[u+1,n]),'availability_source':'research_hourly_receive_proxy'})
    return {'instrument':{'perp_symbol':D.fut[n],'spot_symbol':D.spot[n],'tokens_per_perp_quantity':str(D.meta.iloc[n]['mult'])},
        'position':{'perp_long_qty_token':'0'},'market':{
        'spot_close_usdt_per_token':value(D.arr[1][t-1,n]),'perp_close_usdt_per_token':value(D.arr[4][t-1,n]),
        'trailing_24h_paired_quote_volume_usdt':value(D.dv[t,n]),'settled_r8':value(D.arr[7][t,n]),
        'open_interest_usdt':value(D.oih[t-1,n])},'model_history':history}

def full_request(template,D,ivh,t,n,start_ms):
    v=copy.deepcopy(template);now=start_ms+t*HOUR;row=v['instruments'][0];lite=model_row(D,ivh,t,n,start_ms)
    v.update(request_id=f'research-market-synthetic-account-{t}-{n}',as_of_ms=now,ledger_version=1,mode='paper')
    pf=v['portfolio'];pf.update(initial_capital_usdt='10000000',strategy_marked_pnl_usdt='0',used_entry_capital_usdt='0',
        account_a_free_usdt='0',account_b_free_usdt='10000000',reserved_entry_capital_usdt='0',snapshot_received_at_ms=now,
        account_b_wallet_usdt='10000000',account_b_equity_usdt='10000000',account_b_initial_margin_1x_usdt='0',
        account_b_position_notional_usdt='0',account_b_transferable_usdt='10000000',risk_state='NORMAL',unsettled_transfer_usdt='0')
    i=row['instrument'];i.update(lite['instrument']);i.update(instrument_id=D.spot[n]+'-'+D.fut[n],token=D.spot[n][:-4],
        spot_qty_step_token='0.00000001',perp_qty_step_exchange='0.00000001',spot_min_qty_token='0',perp_min_qty_exchange='0',spot_min_notional_usdt='0',perp_min_notional_usdt='0')
    m=row['market'];m.update(lite['market']);S=Q(m['spot_close_usdt_per_token']);F=Q(m['perp_close_usdt_per_token'])
    events=[u for u in range(max(0,t-24),t+1) if ivh[u,n]>0]
    last=max(u for u in range(t+1) if ivh[u,n]>0)
    m.update(status='OK',observed_at_ms=now,previous_bar_end_ms=now,latest_settlement_time_ms=start_ms+last*HOUR,
        latest_settlement_received_at_ms=start_ms+last*HOUR,latest_raw_rate=value(D.arr[6][last,n]),latest_interval_hours=value(ivh[last,n]),
        settlement_history_complete=True,capacity_view='causal_online',quotes_received_at_ms=now,completed_inputs_received_at_ms=now,
        capacity_window_start_ms=now-HOUR,capacity_window_end_ms=now,capacity_window_complete=True,
        spot_quote_volume_capacity_window_usdt=value(D.arr[2][t-1,n]),perp_quote_volume_capacity_window_usdt=value(D.arr[5][t-1,n]),
        shared_slice_used_usdt='0',shared_slice_reserved_usdt='0',settlement_coverage_start_ms=now-24*HOUR,
        spot_bid_usdt_per_token=format(S*Q('.9999'),'f'),spot_ask_usdt_per_token=format(S*Q('1.0001'),'f'),
        perp_bid_usdt_per_token=format(F*Q('.9999'),'f'),perp_ask_usdt_per_token=format(F*Q('1.0001'),'f'),
        depth_0_2pct_usdt=value(D.arr[9][t-1,n]),depth_1pct_usdt=value(D.arr[10][t-1,n]),depth_source='volume_proxy')
    m.pop('shared_slice_budget_remaining_usdt',None)
    m['settlements']=[{'funding_time_ms':start_ms+u*HOUR,'received_at_ms':start_ms+u*HOUR,'raw_rate':value(D.arr[6][u,n]),'interval_hours':value(ivh[u,n])} for u in events if t-24<u<=t]
    m['raw_settled_sum_24h']=format(sum((Q(e['raw_rate']) for e in m['settlements']),Q(0)),'f')
    m['settled_r8']=format(Q(m['latest_raw_rate'])*8/Q(m['latest_interval_hours']),'f')
    row['position'].update(strategy_sold_spot_qty_token='0',perp_long_qty_exchange='0',perp_long_qty_token='0',exit_locked=False,episode_id=None,
        entry_cost_usdt='0',reserved_entry_cost_usdt='0',reserved_add_qty_token='0',episode_coin_return_due_token='0')
    row['limits']={'inventory_cap_qty_token':format(Q('1000000')/S,'f'),'inventory_available_qty_token':format(Q('1000000')/S,'f')}
    row['risk']={'pause_new':False,'force_exit':False,'notice_received_at_ms':None,'reason_codes':[]}
    row['model_history']=lite['model_history']
    return v
