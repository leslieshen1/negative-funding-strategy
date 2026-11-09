"""Replay synthetic exchange receipts through the real SQLite reference ledger.

Compare every recorded checkpoint with the mock exchange's independently
computed cash and quantities. Databases remain in the supplied output folder.
"""
from pathlib import Path
from decimal import Decimal as D
import argparse,json,sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'support'))
from receipt_ledger import Ledger

BASE=1794182400000;H=3600000
def readlines(p):return [json.loads(x) for x in p.read_text().splitlines() if x]
def event(receipt,index,mult):
    kind=receipt['kind'];at=receipt.get('at_ms',BASE+144*H)
    common=dict(schema_version='nfb.ledger-event.v1',event_key=f'mock-{index:08d}',event_time_ms=at,received_at_ms=at)
    if kind=='fill':
        m=D(1) if receipt['market']=='spot' else mult
        return dict(common,type='fill',instrument_id='mock',account='A' if receipt['market']=='spot' else 'B',
          market=receipt['market'],side=receipt['side'],quantity_exchange=receipt['exchange_quantity'],
          tokens_per_exchange_quantity=str(m),quantity_token=receipt['raw_tokens'],
          price_usdt_per_exchange_quantity=str(D(receipt['price_per_token'])*m),fee_amount=receipt['fee_usdt'],fee_asset='USDT')
    if kind=='funding':return dict(common,type='funding',amount_usdt=receipt['amount_usdt'])
    if kind=='internal_transfer':return dict(common,type='transfer',request_id=f'transfer-{index}',from_account='B',to_account='A',amount=receipt['amount_usdt'],status='SETTLED')
    if kind=='provider_coin_return':return dict(common,type='coin_return',token='MOCK',quantity_token=receipt['raw_tokens'],stage='RETURNED_TO_PROVIDER')
    raise ValueError('Unknown mock receipt kind')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--mock-root',required=True);ap.add_argument('--output',required=True);args=ap.parse_args()
    mock=Path(args.mock_root).resolve();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    if (output/'RESULTS.json').exists():raise RuntimeError('Do not overwrite reconciliation evidence')
    summary=json.loads((mock/'RESULTS.json').read_text());results=[]
    for path in summary['paths_detail']:
        name=f"{path['family']}-{path['path']}";folder=mock/name
        receipts=readlines(folder/'receipts.jsonl');trace=readlines(folder/'decisions.jsonl')
        first=next((x for x in receipts if x['kind']=='fill' and x['market']=='perp'),None)
        # A broken-leg path can have only spot fills. There is then no executed
        # perpetual unit to normalize; do not invent a missing perpetual fill.
        mult=D(first['raw_tokens'])/D(first['exchange_quantity']) if first else D(1)
        opening={'schema_version':'nfb.opening-ledger.v1','source_mode':'synthetic','cash_usdt':{'A':'0','B':'10000000'},
          'owned_token_A':{'MOCK':'30000'},'borrowed_token':{'MOCK':'30000'},'instruments':{'mock':{'token':'MOCK','spot_symbol':'MOCKUSDT','perp_symbol':'MOCKPERPUSDT','tokens_per_perp_quantity':str(mult)}}}
        db=output/(name+'.sqlite');ledger=Ledger(db);ledger.set_opening(opening);cursor=0;checks=0;max_error=D(0);restart_checks=0
        def check(row):
            nonlocal checks,max_error
            state=ledger.state();position=state['perp_positions'].get('mock',{'qty':'0'})
            expected=[D(row['a_cash']),D(row['b_cash']),D(row['sold_token']),D(row['perp_token'])]
            actual=[D(state['cash_usdt']['A']),D(state['cash_usdt']['B']),D('30000')-D(state['owned_token_A']['MOCK']),D(position['qty'])]
            error=max(abs(a-b) for a,b in zip(actual,expected));max_error=max(max_error,error)
            if error>D('.00000001'):raise ValueError(f'{name} hour{row["hour"]} independent ledger mismatch {error}')
            checks+=1
        for row in trace:
            now=BASE+row['hour']*H;repairs=[]
            while cursor<len(receipts) and receipts[cursor].get('at_ms',BASE+144*H)<=now:
                receipt=receipts[cursor];ev=event(receipt,cursor,mult)
                if receipt.get('repair') and receipt['at_ms']==now:repairs.append(ev)
                else:ledger.ingest_many([ev])
                cursor+=1
            check(row)
            if repairs:ledger.ingest_many(repairs)
            if row['hour'] in (36,72,108):
                before=ledger.state();ledger.close();ledger=Ledger(db)
                if ledger.state()!=before:raise ValueError('Restart changed ledger')
                restart_checks+=1
        while cursor<len(receipts):ledger.ingest_many([event(receipts[cursor],cursor,mult)]);cursor+=1
        state=ledger.state();position=state['perp_positions'].get('mock',{'qty':'0'})
        assert D(state['provider_outstanding_token']['MOCK'])==D(path['provider_debt_tokens'])
        assert D(position['qty'])==D(path['remaining_perp_tokens'])
        # A duplicate receipt delivery may change delivery evidence, never cash.
        before=ledger.state();ev=event(receipts[0],0,mult);ev['received_at_ms']+=86400000
        assert ledger.ingest_many([ev])==0
        assert ledger.state()==before
        ledger.close();results.append({'path':name,'checkpoint_checks':checks,'maximum_error_usdt_or_tokens':str(max_error),'restart_checks':restart_checks,'duplicate_cash_unchanged':True})
    result={'passed':True,'origin':'synthetic_mock_receipts','paths':len(results),'checkpoint_checks':sum(x['checkpoint_checks'] for x in results),
       'restart_checks':sum(x['restart_checks'] for x in results),'duplicates_verified':len(results),'details':results,
       'actual_execution_acceptance':False,'submission_allowed':False}
    (output/'RESULTS.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='details'}))

if __name__=='__main__':main()
