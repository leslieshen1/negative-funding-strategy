"""Load frozen research functions into an isolated process; no downloading."""
from pathlib import Path
import json,os,sys

ROOT=Path(__file__).resolve().parents[1]
PHASE=ROOT.parent/'neg-funding-strategy-phase1-20261106'
FROZEN=Path(os.environ.get('NFB_FROZEN_RESEARCH',str(ROOT/'frozen_reference' if (ROOT/'frozen_reference').exists() else ROOT.parent/'neg-funding-strategy-release-20261108/strategy')))
DATA=Path(os.environ.get('NFB_REFERENCE_DATA',str(PHASE/'data')))

def load(data_dir=None):
    data=Path(data_dir or DATA).resolve()
    manifest=json.loads((data/'MANIFEST.json').read_text())
    scratch=ROOT/'audit/research-scratch';scratch.mkdir(exist_ok=True)
    # r7's reference slice cap is 10M for every research symbol. Zero quantities
    # make this public compiler fixture incapable of authorizing any borrowing.
    # Actual received inventories remain solely in the runtime input contract.
    import pandas as pd
    meta=pd.read_csv(data/'ds/dataset_meta.csv')
    capfile=scratch/'public-model-reference-caps.csv'
    pd.DataFrame({'fut_symbol':meta.fut,'cap_usdt':10000000.,'cap_qty_token':0.}).to_csv(capfile,index=False)
    os.environ.update(NFB_DATA=str(data),NFB_DATA_DIR=str(data),NFB_ROOT=str(scratch),
        NFB_OUT=str(scratch)+'/',NFB_DAILY=str(scratch/'daily')+'/',NFB_S='10000000',NFB_DERISK='2',
        NFB_DATA_START=manifest['start_utc'],NFB_DATA_END=manifest['end_utc_exclusive'],
        NFB_CAP_FILE=str(capfile))
    sys.path.insert(0,str(FROZEN/'repro/lib'));sys.path.insert(0,str(FROZEN/'research'))
    import v13lib,run_r7,r7_lib,run_adapt,run_signals
    from causal_inputs import causal_sigma
    D=run_r7.D;D._sg=causal_sigma(D.arr[1]);r7_lib.SIGD=D._sg;run_r7.SIGD=D._sg
    return D,r7_lib,run_r7,run_adapt,run_signals,manifest
