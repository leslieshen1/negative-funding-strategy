"""Analyze supplied fills against strictly pre-order quotes; do not infer causal market impact."""
import argparse,json
from pathlib import Path
from decimal import Decimal as D
from treasury_risk import money,s
from schema_check import strict_load,shape

def analyze(report,benchmarks):
    lookup={}
    for b in benchmarks['rows']:
        key=(b['account'],b['market'],b['symbol'],b['trade_id'])
        if key in lookup:raise ValueError('Duplicate benchmark key')
        lookup[key]=b
    rows=[]
    for f in report['fills']:
        if f['side'] not in ['BUY','SELL']:raise ValueError('Unsupported fill side')
        key=(f['account'],f['market'],f['symbol'],f['trade_id']);b=lookup.get(key)
        result={k:f[k] for k in ['account','market','symbol','trade_id','side','quantity_exchange','quantity_token','fee_asset','fee_amount','event_time_ms']}
        if b is None:result.update(status='MISSING_PREORDER_BENCHMARK',shortfall_usdt=None,shortfall_bps=None);rows.append(result);continue
        if not b['quote_received_at_ms']<=b['order_submit_time_ms']<=f['event_time_ms'] or b['quote_received_at_ms']<b['quote_event_time_ms']:
            result.update(status='INVALID_INFORMATION_TIMING',shortfall_usdt=None,shortfall_bps=None);rows.append(result);continue
        qty=money(f['quantity_exchange']);px=money(f['price_usdt_per_exchange_quantity']);bid=money(b['bid_usdt_per_exchange_quantity']);ask=money(b['ask_usdt_per_exchange_quantity']);mult=money(f['tokens_per_exchange_quantity'])
        if bid<=0 or ask<bid or mult<=0 or qty*mult!=money(f['quantity_token']):raise ValueError('Invalid price or contract units')
        if b['order_submit_time_ms']-b['quote_received_at_ms']>10000:
            result.update(status='STALE_PREORDER_QUOTE',shortfall_usdt=None,shortfall_bps=None);rows.append(result);continue
        mid=(bid+ask)/2;sign=D(1) if f['side']=='BUY' else D(-1);touch=ask if sign>0 else bid
        price_shortfall=qty*sign*(px-mid);spread_component=qty*sign*(touch-mid);fee=money(f['fee_amount'])
        fee_fx=D(1) if f['fee_asset']=='USDT' else None if b.get('fee_asset_usdt_price') is None else money(b['fee_asset_usdt_price'])
        notional=qty*mid
        result.update(status='COMPLETE' if fee_fx is not None else 'FEE_CONVERSION_MISSING',benchmark_notional_usdt=s(notional),price_shortfall_usdt=s(price_shortfall),quoted_spread_component_usdt=s(spread_component),touch_to_fill_shortfall_usdt=s(price_shortfall-spread_component),fee_usdt=None if fee_fx is None else s(fee*fee_fx),shortfall_usdt=None if fee_fx is None else s(price_shortfall+fee*fee_fx),shortfall_bps=None if fee_fx is None else s((price_shortfall+fee*fee_fx)/notional*10000),benchmark_quote_age_at_submit_ms=b['order_submit_time_ms']-b['quote_received_at_ms'])
        rows.append(result)
    groups={}
    for row in rows:
        key='|'.join(row[k] for k in ['account','market','symbol','side']);g=groups.setdefault(key,{'fills':0,'complete_fills':0,'notional_usdt':D(0),'shortfall_usdt':D(0)})
        g['fills']+=1
        if row['status']=='COMPLETE':g['complete_fills']+=1;g['notional_usdt']+=D(row['benchmark_notional_usdt']);g['shortfall_usdt']+=D(row['shortfall_usdt'])
    outgroups={}
    for key,g in groups.items():
        outgroups[key]={**{k:s(v) if isinstance(v,D) else v for k,v in g.items()},'weighted_shortfall_bps':s(g['shortfall_usdt']/g['notional_usdt']*10000) if g['notional_usdt']>0 else None}
    return {'schema_version':'nfb.execution-analytics.v1','declared_receipt_origin':benchmarks.get('source_mode','unverified'),'real_execution_verified':False,'rows':rows,'groups':outgroups,'measures':'Fill-to-preorder-mid implementation shortfall including fees; touch residual also includes market drift/adverse selection.','causal_own_price_or_funding_impact_identified':False,'fill_rate_without_all_order_lifecycle_records':None,'funding_impact_k_estimate':None}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--report',required=True);ap.add_argument('--benchmarks',required=True);ap.add_argument('--output',required=True);args=ap.parse_args()
    report=strict_load(args.report);shape(report,strict_load(Path(__file__).parent/'schemas/execution-report.schema.json'))
    out=analyze(report,strict_load(args.benchmarks));Path(args.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'fills':len(out['rows']),'complete':sum(r['status']=='COMPLETE' for r in out['rows']),'causal_impact_identified':False}))
if __name__=='__main__':main()
