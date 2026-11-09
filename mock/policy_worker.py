"""Isolated frozen RC2 policy: stdin contains one currently visible request only."""
from pathlib import Path
import json,sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from decision import decide
from online_scorer import Scorer

scorer=Scorer.from_path(ROOT/'runtime/models/r7-model-bundle.json')
for line in sys.stdin:
    try:
        packet=json.loads(line)
        if set(packet)!={'request','clock_ms'}:raise ValueError('Unexpected worker packet fields')
        result=decide(packet['request'],scorer,now_ms=packet['clock_ms'],replay=True)
        print(json.dumps({'ok':True,'decision':result},allow_nan=False),flush=True)
    except (ValueError,KeyError,ArithmeticError,TypeError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)}),flush=True)
