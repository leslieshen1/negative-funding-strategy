"""Audit disclosed research identities and baseline; no private input or network."""
from pathlib import Path
from decimal import Decimal
import json,hashlib
ROOT=Path(__file__).resolve().parents[1]
E=ROOT/'mock/evidence/cost-validation'
def d(x):return Decimal(str(x))
def close(a,b,tol='0.000001'):
 if abs(d(a)-d(b))>d(tol):raise ValueError((a,b))
def main():
 x=json.loads((E/'SCENARIOS.json').read_text());rows=x['cases'];by={r['label']:r for r in rows}
 if len(by)!=27 or x['capital_usdt']!=10000000 or x['hours']!=720:raise ValueError('Protocol mismatch')
 if x['runtime_adopted'] or x['actual_execution_receipts'] or x['completed_liquidated_return'] is not None:raise ValueError('Evidence boundary changed')
 for r in rows:
  close(d(r['funding_net_usdt'])+d(r['pair_price_including_impact_usdt'])-d(r['fee_usdt']),r['net_marked_pnl_usdt'])
  close(d(r['market_funding_before_k_usdt'])-d(r['own_funding_k_deduction_usdt']),r['funding_net_usdt'])
  close(d(r['spot_execution_cost_usdt'])+d(r['perpetual_execution_cost_usdt']),r['spread_impact_diagnostic_already_in_pair_usdt'])
  close(d(r['net_marked_pnl_usdt'])/d(10000000)*d(365)/d(30)*d(100),r['simple_annualized_marked_pct'],'0.000000001')
  if r['pre_month_state_error']!=0 or r['ending_open_symbols']<=0:raise ValueError('Starting state or completion mismatch')
 close(by['legacy_k4']['net_marked_pnl_usdt'],108427.3092264263)
 close(by['legacy_k4']['own_funding_k_deduction_usdt'],95445.34748838161)
 if any(x['baseline_parity'].values()):raise ValueError('Baseline parity failed')
 v=json.loads((E/'FRESH_VALIDATION.json').read_text());p=json.loads((E/'PROTOCOL.json').read_text())
 if v['candidate_sha256']!=x['model_sha256'] or p['candidate_model_preexisting_sha256']!=x['model_sha256']:raise ValueError('Model binding mismatch')
 if not v['perpetual_hybrid_allowed_by_fresh_cost_error_gate'] or not v['candidate_model_unchanged']:raise ValueError('Candidate validation failed')
 for m in v['metrics']:
  if m['venue']=='perp' and not m['predefined_gate_passed']:raise ValueError('Perpetual direction failed')
 profile=json.loads((ROOT/'runtime/MODEL_PROFILE.json').read_text())
 if hashlib.sha256((ROOT/'runtime/MODEL_PROFILE.json').read_bytes()).hexdigest()!=p['hashes']['runtime_profile'] or profile['profile']['submission_allowed']:raise ValueError('Frozen production boundary changed')
 print(json.dumps({'passed':True,'aggregate_scenarios_checked':len(rows),'original_month_reproduced':True,'underlying_raw_books_replayed_by_this_command':False,'frozen_strategy_unchanged':True,'production_adopted':False}))
if __name__=='__main__':main()
