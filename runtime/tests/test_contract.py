"""Observable risk/quantity checks with literal synthetic expectations."""
from pathlib import Path
from decimal import Decimal as Q
import copy, json, sys, unittest
from jsonschema import Draft202012Validator, ValidationError
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from validate import validate,validate_pair,strict_load,month_start_ms

def sample(name):return strict_load(ROOT/'examples'/name)
def pair():return sample('01_enter.input.json'),sample('01_enter.output.json')
def set_rate(r,o,rate,interval='8'):
    m=r['instruments'][0]['market'];m['latest_raw_rate']=rate;m['latest_interval_hours']=interval
    m['settled_r8']=format(Q(rate)*8/Q(interval),'f')
    for x in m['settlements']:x['raw_rate']=rate;x['interval_hours']=interval
    m['raw_settled_sum_24h']=format(Q(rate)*len(m['settlements']),'f')
    o['targets'][0]['model_diagnostics']['effective_entry_r8']=m['settled_r8']
def qty(o,current,target,desired=None,action=None):
    t=o['targets'][0];delta=Q(target)-Q(current)
    t.update(current_pair_qty_token=str(current),target_pair_qty_token=str(target),delta_pair_qty_token=format(delta,'f'),
        desired_pair_qty_token=str(target if desired is None else desired),target_perp_qty_exchange=format(Q(target)/1000,'f'))
    if action:t['action']=action
    if delta==0:t['legs']=[]
    else:
        for leg in t['legs']:
            leg['equivalent_raw_tokens']=format(abs(delta),'f');leg['delta_quantity_exchange']=format(abs(delta)/(1000 if leg['market']=='perp' else 1),'f')
            leg['side']=('SELL' if delta>0 else 'BUY') if leg['market']=='spot' else ('BUY' if delta>0 else 'SELL')
            leg['reduce_only']=leg['market']=='perp' and delta<0

class ContractTests(unittest.TestCase):
    def accept(self,r,o):self.assertTrue(validate_pair(r,o,r['as_of_ms']))
    def reject(self,r,o,reason):
        with self.assertRaisesRegex(ValueError,reason):validate_pair(r,o,r['as_of_ms'])
    def test_all_samples_independent_schema_and_semantics(self):
        names={'strategy_input':'strategy-input','strategy_output':'strategy-output','execution_report':'execution-report'}
        for f in sorted((ROOT/'examples').glob('*.json')):
            with self.subTest(f=f.name):
                v=strict_load(f);s=strict_load(ROOT/'schemas'/(names[v['message_type']]+'.schema.json'))
                Draft202012Validator.check_schema(s);Draft202012Validator(s).validate(v);self.assertTrue(validate(v))
                if f.name.endswith('.output.json'):self.accept(sample(f.name.replace('.output.json','.input.json')),v)
    def test_minus_point_one_exactly_fails(self):
        r,o=pair();set_rate(r,o,'-0.001');self.reject(r,o,'strict funding threshold')
    def test_strictly_below_point_one_passes_without_T_minus_one(self):
        r,o=pair();set_rate(r,o,'-0.0010001');self.accept(r,o)
        self.assertEqual((r['as_of_ms']-r['instruments'][0]['market']['latest_settlement_time_ms'])//3600000,4)
    def test_one_hour_and_four_hour_contracts_not_excluded(self):
        for raw,iv in [('-0.0002','1'),('-0.0006','4')]:
            r,o=pair();set_rate(r,o,raw,iv);self.accept(r,o)
    def test_unit_conversion_must_be_exact(self):
        r,o=pair();set_rate(r,o,'-0.0002','1');r['instruments'][0]['market']['settled_r8']='-0.0002'
        self.reject(r,o,'normalization')
    def test_previous_hour_basis_gate(self):
        r,o=pair();r['instruments'][0]['market']['perp_close_usdt_per_token']='99.8';self.reject(r,o,'basis threshold')
    def test_missing_estimate_cannot_open(self):
        r,o=pair();o['targets'][0]['model_diagnostics']['expected_income_rate']=None;self.reject(r,o,'income gate mismatch')
    def test_future_or_current_month_outcomes_rejected(self):
        r,o=pair();o['targets'][0]['model_diagnostics']['income_training_outcomes_end_ms']=month_start_ms(r['as_of_ms'])
        self.reject(r,o,'unavailable outcomes')
    def test_sparse_completed_outcomes_cannot_pass_income_gate(self):
        r,o=pair();o['targets'][0]['model_diagnostics']['income_sample_count']=199
        self.reject(r,o,'too few completed samples')
    def test_adds_use_own_impact_adjusted_rate(self):
        r,o=sample('02_add.input.json'),sample('02_add.output.json');self.accept(r,o)
        o['targets'][0]['model_diagnostics']['effective_entry_r8']='-0.002';self.reject(r,o,'own-impact entry rate')
    def test_condition_failure_can_hold_without_forced_exit(self):
        r,o=sample('03_hold.input.json'),sample('03_hold.output.json');set_rate(r,o,'-0.0001')
        self.accept(r,o);self.assertFalse(o['targets'][0]['exit_locked'])
    def test_positive_raw_sum_requires_exit_lock_even_latest_negative(self):
        r,o=sample('17_partial_exit.input.json'),sample('17_partial_exit.output.json');self.accept(r,o)
        self.assertGreater(Q(r['instruments'][0]['market']['raw_settled_sum_24h']),0)
        self.assertLess(Q(r['instruments'][0]['market']['settled_r8']),0)
        o['targets'][0]['exit_locked']=False;o['targets'][0]['action']='REDUCE';o['targets'][0]['risk_state']='NORMAL'
        self.reject(r,o,'exit lock cancelled')
    def test_exit_goal_zero_is_distinct_from_this_slice_target(self):
        r,o=sample('17_partial_exit.input.json'),sample('17_partial_exit.output.json');self.accept(r,o)
        t=o['targets'][0];self.assertEqual((t['desired_pair_qty_token'],t['target_pair_qty_token'],t['delta_pair_qty_token']),('0','9000','-1000'))
    def test_quote_loss_preserves_lock_without_executable_orders(self):
        r,o=sample('10_missing_quote_exit_locked.input.json'),sample('10_missing_quote_exit_locked.output.json');self.accept(r,o)
        self.assertTrue(o['targets'][0]['exit_locked']);self.assertEqual(o['targets'][0]['delta_pair_qty_token'],'0')
    def test_exit_also_obeys_shared_participation(self):
        r,o=sample('17_partial_exit.input.json'),sample('17_partial_exit.output.json')
        r['instruments'][0]['market']['shared_slice_budget_remaining_usdt']='100000';self.reject(r,o,'shared participation')
    def test_no_orders_with_missing_quotes(self):
        r,o=pair();r['instruments'][0]['market']['spot_bid_usdt_per_token']=None;self.reject(r,o,'current quotes')
    def test_stale_accounts_and_quotes_rejected(self):
        r,o=pair();r['portfolio']['snapshot_received_at_ms']-=10001;self.reject(r,o,'stale accounts')
        r,o=pair();r['instruments'][0]['market']['quotes_received_at_ms']-=10001;self.reject(r,o,'stale quote')
    def test_current_unfinished_capacity_window_rejected(self):
        r,o=pair();r['instruments'][0]['market']['capacity_window_end_ms']+=3600000;self.reject(r,o,'future input')
    def test_stale_capacity_hour_and_incomplete_window_rejected(self):
        r,o=pair();m=r['instruments'][0]['market']
        for key in ['capacity_window_start_ms','capacity_window_end_ms','previous_bar_end_ms']:m[key]-=3600000
        self.reject(r,o,'stale capacity hour')
        r,o=pair();r['instruments'][0]['market']['capacity_window_complete']=False;self.reject(r,o,'incomplete capacity')
    def test_oi_and_volume_limits_cannot_drift_to_old_profile(self):
        r,o=pair();r['instruments'][0]['limits']['max_position_notional_from_oi_usdt']='5000000';self.reject(r,o,'OI cap/profile')
        r,o=pair();r['instruments'][0]['limits']['max_position_notional_from_volume_usdt']='3000000';self.reject(r,o,'volume cap/profile')
    def test_executor_does_not_have_to_calculate_policy_caps(self):
        r,o=pair();row=r['instruments'][0]
        for k in ['per_coin_cap_usdt','max_position_notional_from_oi_usdt','max_position_notional_from_volume_usdt']:del row['limits'][k]
        del row['market']['shared_slice_budget_remaining_usdt'];self.accept(r,o)
    def test_used_and_reserved_hourly_budget_count(self):
        r,o=pair();m=r['instruments'][0]['market'];m['shared_slice_reserved_usdt']='250000';self.reject(r,o,'budget inflated')
    def test_working_orders_count_in_single_coin_capacity(self):
        r,o=pair();pos=r['instruments'][0]['position'];pos['reserved_entry_cost_usdt']='950000';pos['reserved_add_qty_token']='9500'
        r['instruments'][0]['limits']['inventory_cap_qty_token']='20000'
        r['portfolio']['reserved_entry_capital_usdt']='950000';self.reject(r,o,'single coin entry cost')
    def test_loss_reduces_shared_budget_profits_do_not_increase_it(self):
        r,o=pair();r['portfolio']['used_entry_capital_usdt']='9950000';r['portfolio']['strategy_marked_pnl_usdt']='10000000'
        self.reject(r,o,'shared portfolio budget')
        r,o=pair();r['portfolio']['strategy_marked_pnl_usdt']='-9950000';self.reject(r,o,'shared portfolio budget')
    def test_two_coins_cannot_each_spend_the_same_portfolio_remainder(self):
        r,o=pair();r['portfolio']['used_entry_capital_usdt']='9850000'
        row=copy.deepcopy(r['instruments'][0]);row['instrument'].update(instrument_id='SECOND',token='SECOND',spot_symbol='SECONDUSDT',perp_symbol='1000SECONDUSDT');r['instruments'].append(row)
        t=copy.deepcopy(o['targets'][0]);t['instrument_id']='SECOND'
        for leg in t['legs']:leg['symbol']='SECONDUSDT' if leg['market']=='spot' else '1000SECONDUSDT'
        o['targets'].append(t)
        self.reject(r,o,'shared portfolio budget')
    def test_unsettled_transfer_does_not_fund_entry(self):
        r,o=pair();r['portfolio']['account_b_free_usdt']='0';r['portfolio']['account_b_transferable_usdt']='0';r['portfolio']['unsettled_transfer_usdt']='1000000'
        self.reject(r,o,'insufficient settled B')
    def test_equity_loss_reduces_one_x_available_margin(self):
        r,o=pair();r['portfolio']['account_b_equity_usdt']='10000'
        self.reject(r,o,'ignores equity loss')
    def test_unsettled_transfer_does_not_fund_buyback(self):
        r,o=sample('17_partial_exit.input.json'),sample('17_partial_exit.output.json')
        r['portfolio']['account_a_free_usdt']='0';r['portfolio']['unsettled_transfer_usdt']='1000000';self.reject(r,o,'insufficient settled A')
    def test_cash_uses_buy_price_bound_instead_of_displayed_ask(self):
        r,o=sample('17_partial_exit.input.json'),sample('17_partial_exit.output.json')
        r['portfolio']['account_a_free_usdt']='100500'
        for leg in o['targets'][0]['legs']:
            if leg['market']=='spot':leg['limit_price_usdt_per_exchange_quantity']='102'
        self.reject(r,o,'insufficient settled A')
    def test_transfer_wait_has_no_orders_and_preserves_lock(self):
        r,o=sample('19_transfer_wait.input.json'),sample('19_transfer_wait.output.json');self.accept(r,o)
        self.assertEqual(o['targets'][0]['delta_pair_qty_token'],'0');self.assertTrue(o['targets'][0]['exit_locked'])
    def test_unmatched_legs_require_reconcile(self):
        r,o=sample('08_reconcile.input.json'),sample('08_reconcile.output.json');self.accept(r,o)
        o['targets'][0]['action']='HOLD';self.reject(r,o,'requires reconciliation')
    def test_pause_prohibits_adds(self):
        r,o=pair();r['portfolio']['risk_state']='PAUSE_NEW';self.reject(r,o,'risk pause')
    def test_notice_time_required_for_forced_exit(self):
        r,o=pair();r['instruments'][0]['risk']['force_exit']=True;self.reject(r,o,'lacks notice time')
    def test_exact_joint_lot_not_max_step(self):
        r,o=pair();i=r['instruments'][0]['instrument'];i['spot_qty_step_token']='0.03';i['perp_qty_step_exchange']='0.00002'
        qty(o,'0','1000.02');self.accept(r,o)
        qty(o,'0','1000.01');self.reject(r,o,'paired lot')
    def test_minimum_notional_cannot_be_ignored(self):
        r,o=pair();r['instruments'][0]['instrument']['perp_min_notional_usdt']='1000000';self.reject(r,o,'minimum order notional')
    def test_duplicate_settlement_zero_rate_and_late_old_event(self):
        r,o=pair();m=r['instruments'][0]['market'];m['settlements'].append(copy.deepcopy(m['settlements'][0]));self.reject(r,o,'duplicate funding settlement')
        r,o=pair();m=r['instruments'][0]['market'];m['settlements'][0]['raw_rate']='0';m['raw_settled_sum_24h']='-0.004';self.accept(r,o)
        m['latest_settlement_time_ms']=m['settlements'][0]['funding_time_ms'];m['latest_settlement_received_at_ms']=m['settlements'][0]['received_at_ms'];self.reject(r,o,'latest economic event')
        r,o=pair();m=r['instruments'][0]['market'];m.update(settlements=[],latest_settlement_time_ms=r['as_of_ms']-48*3600000,
            latest_settlement_received_at_ms=r['as_of_ms']-48*3600000,raw_settled_sum_24h='0')
        self.reject(r,o,'stale settled signal')
    def test_exit_state_cannot_be_silently_omitted(self):
        r,o=pair();o['targets']=[];self.reject(r,o,'omitted instrument state')
    def test_new_profile_id_hash_and_submission_cannot_be_overridden(self):
        r,o=pair();o['model']['strategy_id']='nfb.v1.frozen.6cd6c23';self.reject(r,o,'constant mismatch')
        r,o=pair();o['model']['profile_sha256']='0'*64;self.reject(r,o,'constant mismatch')
        r,o=pair();o['submission_allowed']=True;self.reject(r,o,'constant mismatch|cannot authorize')
    def test_independent_schema_rejects_exponents_and_integer_boolean(self):
        r,o=pair();o['targets'][0]['desired_pair_qty_token']='1e3'
        validator=Draft202012Validator(strict_load(ROOT/'schemas/strategy-output.schema.json'))
        with self.assertRaises(ValidationError):validator.validate(o)
        self.reject(r,o,'invalid numeric')
        r,o=pair();o['submission_allowed']=0
        with self.assertRaises(ValidationError):validator.validate(o)
        self.reject(r,o,'wrong type')
    def test_expiry_and_ledger_version(self):
        r,o=pair();o['ledger_version']+=1;self.reject(r,o,'ledger_version mismatch')
        r,o=pair()
        with self.assertRaisesRegex(ValueError,'expired'):validate_pair(r,o,o['valid_until_ms'])
    def test_duplicate_fill_and_funding_receipt(self):
        v=sample('07_partial.execution-report.json');v['fills'].append(copy.deepcopy(v['fills'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate fill'):validate(v)
        v=sample('11_actual_funding.execution-report.json');v['funding_receipts'].append(copy.deepcopy(v['funding_receipts'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate funding_receipts'):validate(v)
    def test_pending_receipt_is_not_settled(self):
        v=sample('09_transfer_pending.execution-report.json');v['transfer_receipts'][0]['status']='SETTLED'
        with self.assertRaisesRegex(ValueError,'status/time'):validate(v)

if __name__=='__main__':unittest.main(verbosity=2)
