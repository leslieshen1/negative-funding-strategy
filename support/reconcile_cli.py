"""Read-only with respect to execution: ingest supplied receipts into an explicit local database."""
import argparse,json,hashlib
from decimal import Decimal as D
from pathlib import Path
from receipt_ledger import Ledger,report_events,canonical

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--database',required=True);ap.add_argument('--opening',required=True);ap.add_argument('--reports',nargs='*',default=[]);ap.add_argument('--events',nargs='*',default=[]);ap.add_argument('--expected-state');ap.add_argument('--output',required=True);args=ap.parse_args()
    opening=json.loads(Path(args.opening).read_text()); mapping={}
    for iid,spec in opening['instruments'].items():
        mapping[('A','spot',spec['spot_symbol'])]=iid;mapping[('B','perp',spec['perp_symbol'])]=iid
    audit=Path(args.database).parent/'receipt-evidence';audit.mkdir(exist_ok=True,parents=True); failures=[];inserted=0
    with Ledger(args.database) as ledger:
        ledger.set_opening(opening)
        for file,kind in [(p,'report') for p in args.reports]+[(p,'events') for p in args.events]:
            raw=Path(file).read_bytes();ident=hashlib.sha256(raw).hexdigest();evidence=audit/(ident+'.json')
            if not evidence.exists():evidence.write_bytes(raw)
            try:
                data=json.loads(raw);events=report_events(data,mapping) if kind=='report' else data if isinstance(data,list) else [data]
                inserted+=ledger.ingest_many(events)
            except (ValueError,KeyError,TypeError) as exc:failures.append({'source_file':str(file),'sha256':ident,'error':str(exc)})
        retry=ledger.retry_quarantine();state=ledger.state()
        if args.expected_state:
            expected=json.loads(Path(args.expected_state).read_text());diff=[]
            for key in ['cash_usdt','owned_token_A','provider_outstanding_token','hedge_error_token']:
                if expected.get(key)!=state.get(key):diff.append({'field':key,'expected':expected.get(key),'computed':state.get(key)})
        else:diff=None
        unsettled=any(c['status']=='PENDING_RECONCILIATION' for c in state['quarantine'])
        hedge_gap=any(D(v)!=0 for v in state['hedge_error_token'].values());negative_cash=any(D(v)<0 for v in state['cash_usdt'].values());unpriced_fee=any(x.startswith('UNPRICED_FEE_ASSET') for x in state['warnings'])
        result={'schema_version':'nfb.reconciliation.v1','status':'NEEDS_RECONCILIATION' if failures or unsettled or diff or hedge_gap or negative_cash or unpriced_fee else 'LOCAL_LEDGER_COMPUTED','inserted_events':inserted,'state':state,'differences':diff,'intake_failures':failures,'quarantine_retry':retry,'real_receipts_supplied':False,'real_execution_acceptance':False,'submission_allowed':False}
        # Actual origin is deliberately not inferred from a file name or a synthetic fixture.
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:result[k] for k in ['status','inserted_events','real_execution_acceptance']},ensure_ascii=False))
if __name__=='__main__':main()
