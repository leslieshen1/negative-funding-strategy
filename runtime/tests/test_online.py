"""Scalar model unit fixtures plus real-history/synthetic-account decision paths."""
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal as Q
import copy,hashlib,json,sys,unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from online_scorer import Scorer,HOUR
from decision import decide
from validate import strict_load,validate_pair,MODEL

def doc(bundle):
    raw=json.dumps(bundle,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return {'bundle':bundle,'bundle_sha256':hashlib.sha256(raw).hexdigest()}
def fake():
    request=strict_load(ROOT/'examples/01_enter.input.json');now=request['as_of_ms'];row=request['instruments'][0]
    start=int(datetime.fromtimestamp(now/1000,tz=timezone.utc).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
    sample={'n':300,'mean':.02,'max_outcome_end_ms':start-1}
    bundle={'format_version':'1.0.0','published_at_ms':now-1000,'cost_calibration_last_day':'2026-11-01',
        'funding_bucket_edges':[.0005,.001,.002,.005],'basis_bucket_edges':[-.012,-.006],
        'income_models':[{'valid_from_ms':start,'valid_until_ms':start+30*24*HOUR,
            'funding':[copy.deepcopy(sample) for _ in range(4)],'basis':[copy.deepcopy(sample) for _ in range(3)],
            'joint':[[copy.deepcopy(sample) for _ in range(3)] for _ in range(4)]}],
        'asset_priors':{'1000TESTUSDT':{'spot_symbol':'TESTUSDT','tokens_per_perp_quantity':'1000','half_spread_spot':.0003,'half_spread_perp':.00015}},
        'calibration':[{'symbol':'1000TESTUSDT','sample_day':'2026-11-01','available_at_ms':now-72*HOUR,'y_kyle':1.,'half_spread_perp':.00015}]}
    row['model_history']=[{'bar_end_ms':now-j*HOUR,'received_at_ms':now-j*HOUR,
        'spot_close_usdt_per_token':'100','perp_close_usdt_per_token':'99.5','oi_usdt':'50000000','availability_source':'observed'} for j in range(25,-1,-1)]
    return request,bundle

class ScalarTests(unittest.TestCase):
    def test_literal_constant_prices_cost_and_joint_mean(self):
        r,b=fake();d,reason=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms'])
        self.assertEqual(d['status'],'VALID');self.assertEqual(Q(d['sigma_24h']),0)
        self.assertEqual(Q(d['round_trip_cost_rate']),Q('.0039'))
        self.assertEqual(Q(d['expected_income_rate']),Q('.02'));self.assertTrue(d['r7_gate_passed'])
        self.assertEqual(d['income_sample_count'],300);self.assertEqual(d['income_estimate_source'],'joint')
    def test_marginal_fallback_uses_both_sample_counts(self):
        r,b=fake();sc=Scorer(doc(b));est,_=sc.estimate('-.002','-.005',r['as_of_ms']);j,k=est['fb'],est['bb']
        m=b['income_models'][0];m['joint'][j][k]['mean']=None;m['funding'][j].update(mean=.01,n=400);m['basis'][k].update(mean=.002,n=350)
        d,_=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms'])
        self.assertEqual(d['income_estimate_source'],'marginal_fallback');self.assertEqual(d['income_sample_count'],350)
        self.assertAlmostEqual(float(d['expected_income_rate']),.012)
    def test_sparse_cells_never_fabricate_income(self):
        r,b=fake()
        for m in b['income_models']:
            for x in m['funding']+m['basis']+[x for a in m['joint'] for x in a]:x.update(mean=None,n=199)
        d,why=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms'])
        self.assertEqual(d['status'],'UNAVAILABLE');self.assertIsNone(d['expected_income_rate']);self.assertIn('INCOME_SAMPLE_SHORTAGE',why)
    def test_missing_month_is_not_forward_filled(self):
        r,b=fake();b['income_models'][0]['valid_until_ms']=r['as_of_ms']
        d,why=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms']);self.assertIn('MODEL_MONTH_UNAVAILABLE',why)
    def test_missing_history_and_future_history_fail_closed(self):
        r,b=fake();row=r['instruments'][0];row.pop('model_history')
        d,why=Scorer(doc(b)).score(row,r['as_of_ms']);self.assertIn('MODEL_HISTORY_MISSING',why)
        r,b=fake();r['instruments'][0]['model_history'][-1]['received_at_ms']+=1
        d,why=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms']);self.assertIn('MODEL_HISTORY_FUTURE',why)
    def test_duplicate_and_gapped_bars_do_not_pass(self):
        r,b=fake();row=r['instruments'][0];row['model_history'].append(copy.deepcopy(row['model_history'][-1]))
        _,why=Scorer(doc(b)).score(row,r['as_of_ms']);self.assertIn('MODEL_HISTORY_DUPLICATE_OR_UNSORTED',why)
        r,b=fake();r['instruments'][0]['model_history'].pop(10)
        _,why=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms']);self.assertIn('MODEL_HISTORY_GAP',why)
    def test_receive_proxy_requires_explicit_replay(self):
        r,b=fake();row=r['instruments'][0]
        for h in row['model_history']:h['availability_source']='research_hourly_receive_proxy'
        _,why=Scorer(doc(b)).score(row,r['as_of_ms']);self.assertIn('MODEL_HISTORY_RECEIVE_PROXY',why)
        d,_=Scorer(doc(b)).score(row,r['as_of_ms'],replay=True);self.assertEqual(d['status'],'VALID')
    def test_actual_model_publication_time_is_enforced(self):
        r,b=fake();b['published_at_ms']=r['as_of_ms']+1
        _,why=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms']);self.assertIn('MODEL_NOT_YET_PUBLISHED',why)
    def test_appending_future_calibration_changes_no_past_numbers(self):
        r,b=fake();s1=Scorer(doc(b));d1,_=s1.score(r['instruments'][0],r['as_of_ms'])
        b['calibration'].append({'symbol':'1000TESTUSDT','sample_day':'2027-01-01','available_at_ms':r['as_of_ms']+24*HOUR,'y_kyle':1000,'half_spread_perp':10})
        d2,_=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms'])
        for key in ['expected_income_rate','round_trip_cost_rate','calibration_y','calibration_spread_multiplier','calibration_data_last_available_ms','r7_gate_passed']:self.assertEqual(d1[key],d2[key])
    def test_bundle_hash_mismatch_is_rejected(self):
        _,b=fake();v=doc(b);v['bundle']['sample_minimum']=1
        with self.assertRaisesRegex(ValueError,'bundle hash'):Scorer(v)
    def test_funding_and_basis_bucket_boundaries_follow_frozen_digitize(self):
        r,b=fake();model=Scorer(doc(b))
        a,_=model.estimate('-.001','-.012',r['as_of_ms']);self.assertEqual((a['fb'],a['bb']),(1,1))
        a,_=model.estimate('-.002','-.006',r['as_of_ms']);self.assertEqual((a['fb'],a['bb']),(2,2))
    def test_adl_proxy_is_computed_from_price_and_coin_oi(self):
        r,b=fake();h=r['instruments'][0]['model_history'];h[20]['perp_close_usdt_per_token']='119.4'
        d,_=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms']);self.assertTrue(d['adl_entry_blocked'])
    def test_incomplete_adl_data_is_not_assumed_safe(self):
        r,b=fake();r['instruments'][0]['model_history'][10]['oi_usdt']=None
        d,why=Scorer(doc(b)).score(r['instruments'][0],r['as_of_ms']);self.assertEqual(d['status'],'UNAVAILABLE');self.assertIn('ADL_HISTORY_INCOMPLETE',why)

class DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=ROOT/'runtime_examples/01_historical_market_enter.input.json'
        cls.scorer=Scorer.from_path(ROOT/'models/r7-model-bundle.json')
    def request(self):return strict_load(self.source)
    def run_decision(self,r):return decide(r,self.scorer,now_ms=r['as_of_ms'],replay=True)
    def test_actual_historical_market_generates_valid_enter(self):
        r=self.request();o=self.run_decision(r);self.assertTrue(validate_pair(r,o,r['as_of_ms']))
        self.assertEqual(o['targets'][0]['action'],'ENTER');self.assertFalse(o['submission_allowed'])
        self.assertGreater(Q(o['targets'][0]['delta_pair_qty_token']),0)
    def test_same_payload_has_same_decision_and_does_not_mutate_input(self):
        r=self.request();before=copy.deepcopy(r);a=self.run_decision(r);b=self.run_decision(r)
        self.assertEqual(a,b);self.assertEqual(r,before)
    def test_exact_rate_threshold_holds(self):
        r=self.request();m=r['instruments'][0]['market']
        for e in m['settlements']:e['raw_rate']=str(Q('-.001')*Q(e['interval_hours'])/8)
        m['latest_raw_rate']=m['settlements'][-1]['raw_rate'];m['settled_r8']='-0.001';m['raw_settled_sum_24h']=str(sum((Q(e['raw_rate']) for e in m['settlements']),Q(0)))
        o=self.run_decision(r);self.assertEqual(o['targets'][0]['action'],'HOLD');self.assertFalse(o['targets'][0]['legs'])
    def test_missing_model_history_stops_new_exposure(self):
        r=self.request();r['instruments'][0].pop('model_history');o=self.run_decision(r)
        self.assertEqual(o['targets'][0]['action'],'HOLD');self.assertIn('MODEL_HISTORY_MISSING',o['targets'][0]['reason_codes'])
    def hold_position(self,r,qty='1000',locked=True):
        row=r['instruments'][0];p=row['position'];m=row['market'];q=Q(qty);mult=Q(row['instrument']['tokens_per_perp_quantity'])
        p.update(strategy_sold_spot_qty_token=qty,perp_long_qty_token=qty,perp_long_qty_exchange=format(q/mult,'f'),
            entry_cost_usdt=format(q*Q(m['spot_close_usdt_per_token']),'f'),episode_coin_return_due_token=qty,episode_id='synthetic-open-episode',exit_locked=locked)
        r['portfolio']['used_entry_capital_usdt']=p['entry_cost_usdt']
        r['portfolio']['account_b_initial_margin_1x_usdt']=format(q*Q(m['perp_close_usdt_per_token']),'f')
        r['portfolio']['account_b_position_notional_usdt']=r['portfolio']['account_b_initial_margin_1x_usdt']
        r['portfolio']['account_b_free_usdt']='9000000';r['portfolio']['account_b_transferable_usdt']='9000000'
        return row
    def test_locked_exit_works_without_income_model_history(self):
        r=self.request();row=self.hold_position(r);row.pop('model_history');r['portfolio']['account_a_free_usdt']='1000000'
        o=self.run_decision(r);t=o['targets'][0];self.assertEqual(t['action'],'EXIT');self.assertTrue(t['exit_locked']);self.assertGreater(len(t['legs']),0)
        self.assertIsNone(t['model_diagnostics']['expected_income_rate'])
    def test_no_buyback_cash_requests_transfer_without_using_it(self):
        r=self.request();self.hold_position(r);r['portfolio']['account_a_free_usdt']='0'
        o=self.run_decision(r);t=o['targets'][0];self.assertTrue(t['exit_locked']);self.assertFalse(t['legs']);self.assertTrue(o['transfer_requests'])
    def test_new_positive_24h_sum_locks_exit_even_latest_rate_is_negative(self):
        r=self.request();row=self.hold_position(r,locked=False);m=row['market'];r['portfolio']['account_a_free_usdt']='1000000'
        self.assertGreater(len(m['settlements']),1)
        m['settlements'][0]['raw_rate']='0.1'
        m['raw_settled_sum_24h']=format(sum((Q(e['raw_rate']) for e in m['settlements']),Q(0)),'f')
        o=self.run_decision(r);self.assertTrue(o['targets'][0]['exit_locked']);self.assertEqual(o['targets'][0]['action'],'EXIT')
    def test_known_capacity_shrink_reduces_without_funding_exit(self):
        r=self.request();row=self.hold_position(r,locked=False);row['market']['open_interest_usdt']='0';r['portfolio']['account_a_free_usdt']='1000000'
        o=self.run_decision(r);self.assertEqual(o['targets'][0]['action'],'REDUCE');self.assertFalse(o['targets'][0]['exit_locked'])
    def test_margin_repair_intent_does_not_become_spendable_B_cash(self):
        r=self.request();self.hold_position(r,locked=False);pf=r['portfolio'];pf.update(account_b_wallet_usdt='10',account_b_equity_usdt='10',
            account_b_free_usdt='0',account_b_transferable_usdt='0',account_a_free_usdt='1000000')
        o=self.run_decision(r);self.assertFalse(o['targets'][0]['legs']);self.assertEqual(o['targets'][0]['risk_state'],'PAUSE_NEW')
        self.assertEqual(o['transfer_requests'][0]['from_account'],'A');self.assertEqual(o['transfer_requests'][0]['to_account'],'B')
        self.assertIn('ONE_X_MARGIN_SHORTFALL_REQUIRES_REPAIR',o['reason_codes'])
    def test_pending_transfer_is_not_requested_again_or_spent(self):
        r=self.request();self.hold_position(r);r['portfolio']['unsettled_transfer_usdt']='1000000'
        o=self.run_decision(r);self.assertFalse(o['transfer_requests']);self.assertFalse(o['targets'][0]['legs'])
    def test_lost_quote_preserves_exit_lock(self):
        r=self.request();row=self.hold_position(r);row['market']['spot_bid_usdt_per_token']=None
        o=self.run_decision(r);t=o['targets'][0];self.assertTrue(t['exit_locked']);self.assertEqual(t['desired_pair_qty_token'],'0');self.assertFalse(t['legs'])
    def test_partial_actual_legs_are_reconciled(self):
        r=self.request();row=self.hold_position(r);row['position']['perp_long_qty_token']='600'
        row['position']['perp_long_qty_exchange']=format(Q(600)/Q(row['instrument']['tokens_per_perp_quantity']),'f')
        o=self.run_decision(r);t=o['targets'][0];self.assertEqual(t['action'],'RECONCILE');self.assertEqual(t['hedge_error_qty_token'],'-400');self.assertFalse(t['legs'])
    def test_stale_request_rejected(self):
        r=self.request()
        with self.assertRaisesRegex(ValueError,'STALE'):decide(r,self.scorer,now_ms=r['as_of_ms']+60000,replay=True)
    def test_replay_flag_never_accepts_live_mode(self):
        r=self.request();r['mode']='live'
        with self.assertRaisesRegex(ValueError,'replay flag'):decide(r,self.scorer,now_ms=r['as_of_ms'],replay=True)
    def test_mismatched_model_artifact_cannot_be_used(self):
        r=self.request();_,fake_model=fake();model=Scorer(doc(fake_model))
        with self.assertRaisesRegex(ValueError,'not pinned'):decide(r,model,now_ms=r['as_of_ms'],replay=True)
    def test_pause_blocks_new_but_is_not_an_exit_signal(self):
        r=self.request();self.hold_position(r,locked=False);r['portfolio']['risk_state']='PAUSE_NEW'
        o=self.run_decision(r);self.assertEqual(o['targets'][0]['action'],'HOLD');self.assertFalse(o['targets'][0]['exit_locked'])
    def test_two_coins_share_one_remaining_capital_budget(self):
        r=self.request();first=r['instruments'][0];other=copy.deepcopy(first)
        symbol=next(s for s,p in self.scorer.bundle['asset_priors'].items() if s!=first['instrument']['perp_symbol'] and Q(p['tokens_per_perp_quantity'])==1)
        prior=self.scorer.bundle['asset_priors'][symbol]
        other['instrument'].update(instrument_id='SYNTHETIC_SECOND',token=prior['spot_symbol'][:-4],spot_symbol=prior['spot_symbol'],perp_symbol=symbol)
        r['instruments'].append(other)
        for row in r['instruments']:
            for h in row['model_history']:
                h['spot_close_usdt_per_token']=row['market']['spot_close_usdt_per_token']
        before=self.run_decision(r);self.assertEqual(sum(bool(t['legs']) for t in before['targets']),2)
        r['portfolio']['used_entry_capital_usdt']='9999900'
        after=self.run_decision(r)
        spent=sum((Q(t['delta_pair_qty_token'])*Q(row['market']['spot_close_usdt_per_token']) for row,t in zip(r['instruments'],after['targets'])),Q(0))
        self.assertLessEqual(spent,Q(100));self.assertEqual(sum(bool(t['legs']) for t in after['targets']),1)

if __name__=='__main__':unittest.main(verbosity=2)
