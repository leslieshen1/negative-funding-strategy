"""Replay frozen RC2 on explicitly supplied historical data and private inventory.

This is an already-observed historical regression, never a new blind OOS test.
No downloads, active ledger writes, model fitting or parameter selection.
"""
from pathlib import Path
import argparse,hashlib,json,os,sys

ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for part in iter(lambda:f.read(4*1024*1024),b''):h.update(part)
    return h.hexdigest()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data',required=True);ap.add_argument('--inventory',required=True);ap.add_argument('--output',required=True);args=ap.parse_args()
    data=Path(args.data).resolve();inventory=Path(args.inventory).resolve();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'RESULTS.json').exists():raise RuntimeError('Do not overwrite historical evidence')
    source=ROOT/'frozen_reference/research/run_study.py'
    protocol={'kind':'previously_seen_historical_regression','blind_OOS':False,'strategy_id':'nfb.strategy.20261108.rc2',
      'input_hashes':{n:sha(data/n) for n in ['ds/dataset.npz','ds/dataset_meta.csv','calib/ag.csv','calib/bd.csv']},
      'private_inventory_sha256':sha(inventory),'source_sha256':sha(source),'capital_usdt':10000000,
      'tail_hours':216,'model_or_threshold_change':False,'submission_allowed':False}
    (out/'PROTOCOL.json').write_text(json.dumps(protocol,indent=2)+'\n')
    os.environ.update(NFB_PHASE1_ROOT=str(out/'context'),NFB_CAP_FILE=str(inventory),NUMBA_CACHE_DIR=str(out/'numba-cache'))
    (out/'context').mkdir(exist_ok=True)
    sys.argv=[str(source),'--data',str(data),'--suite','concentration','--profile','candidate_diversified','--output',str(out/'replay')]
    text=source.read_text();marker="run('causal_reference',reference)\n";assert text.count(marker)==1
    ctx={'__file__':str(source),'__name__':'historical_regression_context'}
    exec(compile(text.split(marker)[0],str(source)+':prefix','exec'),ctx)
    RR,RA=ctx['RR'],ctx['RA'];RR.WINS['M30']=(RA.HABS('2026-09-28'),RA.HABS('2026-10-28'))
    ctx['run']('frozen_RC2',ctx['reference'],windows=('IS','HO','FY','M30'),oi=.025,gate=2.)
    rows=ctx['results'];expected=json.loads((ROOT/'runtime/MODEL_PROFILE.json').read_text())['profile']['frozen_resolved_engine_parameters']
    fy=next(x for x in rows if x['window']=='FY')
    assert all(abs(fy['resolved_engine_parameters'][k]-v)<1e-12 for k,v in expected.items()),'RC2 engine parameters changed'
    record={'protocol':protocol,'cases':rows,'all_terminal_obligations_zero':all(x['terminal_residual_symbols']==0 for x in rows),
      'all_96_parameters_matched':True,'real_execution_verified':False}
    (out/'RESULTS.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'passed':True,'runs':len(rows),'annual_completed_pct':fy['ann_completed_pct'],'net_usdt':fy['net_usdt'],'blind_OOS':False}),flush=True)

if __name__=='__main__':main()
