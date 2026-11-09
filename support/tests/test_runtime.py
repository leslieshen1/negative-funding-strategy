import copy,json,sys,tempfile,threading,unittest
from pathlib import Path
from decimal import Decimal as D
RUNTIME=Path(__file__).resolve().parents[1];sys.path.insert(0,str(RUNTIME))
from treasury_risk import decide,InputError,product_collateral
from receipt_ledger import Ledger,Conflict,ReconcileError,report_events
from schema_check import strict_load

def request():return json.loads((RUNTIME/'examples/01_eligible.input.json').read_text())
def opening():return json.loads((RUNTIME/'examples/opening.synthetic.json').read_text())
def event(key,kind,at=1,**kw):return dict(schema_version='nfb.ledger-event.v1',event_key=key,type=kind,event_time_ms=at,received_at_ms=at+100,**kw)
def fill(key,market,side,qty,price,at=1,fee='0',fee_asset='USDT'):
    mult='1' if market=='spot' else '1000'
    return event(key,'fill',at,instrument_id='1000TESTUSDT',account='A' if market=='spot' else 'B',market=market,side=side,quantity_exchange=qty,tokens_per_exchange_quantity=mult,quantity_token=str(D(qty)*D(mult)),price_usdt_per_exchange_quantity=price,fee_amount=fee,fee_asset=fee_asset)

class LedgerTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'ledger.sqlite';self.l=Ledger(self.p);self.l.set_opening(opening())
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def test_E01_request_repeat_returns_original(self):
        self.assertEqual(self.l.cache_request('r',{'v':1},{'id':'d'}),({'id':'d'},True))
        self.assertEqual(self.l.cache_request('r',{'v':1},{'id':'wrong'}),({'id':'d'},False))
    def test_E02_request_conflict_rejected(self):
        self.l.cache_request('r',{'v':1},{})
        with self.assertRaises(Conflict):self.l.cache_request('r',{'v':2},{})
    def test_E03_restart_keeps_consumption(self):
        self.l.reserve('d',{'a':1},0,'A',0,'100','100','100','100',1000);self.l.consume('d','40',10,'order-1');self.l.close();self.l=Ledger(self.p)
        self.assertFalse(self.l.reserve('d',{'a':1},0,'A',0,'100','100','100','100',1000))
        self.assertFalse(self.l.consume('d','40',20,'order-1'))
        with self.assertRaises(Conflict):self.l.consume('d','61',20,'order-2')
    def test_E04_concurrent_version_reservation_one_winner(self):
        barrier=threading.Barrier(2);results=[]
        def worker(n):
            with Ledger(self.p) as ledger:
                barrier.wait()
                try:ledger.reserve('d'+str(n),{'n':n},0,'A',0,'80','100','100','100',1000);results.append('PASS')
                except Conflict:results.append('REJECT')
        a=threading.Thread(target=worker,args=(1,));b=threading.Thread(target=worker,args=(2,));a.start();b.start();a.join();b.join()
        self.assertCountEqual(results,['PASS','REJECT'])
    def test_E05_coins_share_portfolio_budget(self):
        self.l.reserve('a',{},0,'A',0,'60','100','100','100',1000)
        with self.assertRaises(Conflict):self.l.reserve('b',{},1,'B',0,'60','100','100','100',1000)
    def test_E06_cancel_pending_keeps_reservation(self):
        self.l.reserve('a',{},0,'A',0,'80','100','100','100',1000);self.l.cancel('a')
        with self.assertRaises(Conflict):self.l.reserve('b',{},2,'B',0,'30','100','100','100',1000)
        self.l.cancel('a',confirmed=True);self.assertTrue(self.l.reserve('b',{},3,'B',0,'30','100','100','100',1000))
    def test_E07_expired_permission_but_late_fill_recorded(self):
        self.l.reserve('a',{},0,'A',0,'100','100','100','100',10)
        with self.assertRaises(Conflict):self.l.consume('a','1',11,'order-1')
        e=fill('late','spot','SELL','100','1',20);e['permission_expired_at_ms']=10;self.l.ingest_many([e])
        self.assertEqual(self.l.state()['cash_usdt']['A'],'100');self.assertIn('LATE_FILL_RECORDED:late',self.l.state()['warnings'])
    def test_E08_duplicate_cash_event_across_reports(self):
        e=event('fund1','funding',amount_usdt='12.5');self.assertEqual(self.l.ingest_many([e]),1)
        e['received_at_ms']=1000;self.assertEqual(self.l.ingest_many([e]),0)
        self.assertEqual(self.l.state()['cash_usdt']['B'],'10000012.5')
    def test_E09_out_of_order_snapshot_and_receipt_recovery(self):
        self.l.accept_snapshot(2,{'x':2})
        with self.assertRaises(Conflict):self.l.accept_snapshot(1,{'x':1})
        sell=fill('close','perp','SELL','0.1','1100',10)
        with self.assertRaises(ReconcileError):self.l.ingest_many([sell])
        self.l.ingest_many([fill('open','perp','BUY','0.1','1000',1)]);result=self.l.retry_quarantine()
        self.assertTrue(result[0]['recovered']);self.assertEqual(D(self.l.state()['realized_perp_pnl_usdt']),D('10.0'))
    def test_E10_partial_position_funding_uses_actual_amount(self):
        self.l.ingest_many([fill('s','spot','SELL','200','1'),fill('p','perp','BUY','0.1','1000'),event('f','funding',2,amount_usdt='7.25')])
        state=self.l.state();self.assertEqual(D(state['hedge_error_token']['1000TESTUSDT']),D('-100'));self.assertEqual(state['funding_received_usdt'],'7.25')
    def test_transfer_pending_and_failed_debit_are_not_cash(self):
        tr=dict(request_id='x',from_account='B',to_account='A',amount='1000')
        self.l.ingest_many([event('p','transfer',1,status='PENDING',**tr)])
        self.assertEqual(self.l.state()['cash_usdt']['A'],'0')
        self.l.ingest_many([event('d','transfer',2,status='DEBITED',**tr),event('f','transfer',3,status='FAILED',**tr)])
        self.assertEqual(self.l.state()['internal_transfer_in_flight_usdt'],'1000');self.assertEqual(self.l.state()['cash_usdt']['B'],'9999000')
        self.l.ingest_many([event('r','transfer',4,status='REFUNDED',**tr)]);self.assertEqual(self.l.state()['cash_usdt']['B'],'10000000')
    def test_transfer_conserves_combined_cash(self):
        self.l.ingest_many([event('t','transfer',status='SETTLED',request_id='x',from_account='B',to_account='A',amount='100')])
        st=self.l.state();self.assertEqual(sum(map(D,st['cash_usdt'].values())),D('10000000'))
    def test_cancel_confirmation_does_not_free_consumed_risk(self):
        self.l.reserve('a',{},0,'A',0,'100','100','100','100',1000);self.l.consume('a','40',1,'slice1');self.l.cancel('a',confirmed=True)
        with self.assertRaises(Conflict):self.l.reserve('b',{},3,'B',0,'80','100','100','100',1000)
    def test_RC2_adapter_roundtrip_cash_and_coins(self):
        report=json.loads((RUNTIME/'examples/roundtrip.synthetic.execution-report.json').read_text());mapping={('A','spot','TESTUSDT'):'1000TESTUSDT',('B','perp','1000TESTUSDT'):'1000TESTUSDT'}
        events=report_events(report,mapping);self.l.ingest_many(events);st=self.l.state()
        self.assertEqual(D(st['cash_usdt']['A']),D('7.8788'));self.assertEqual(D(st['cash_usdt']['B']),D('9999996.93536'))
        self.assertEqual(D(st['owned_token_A']['TEST']),0);self.assertEqual(D(st['provider_outstanding_token']['TEST']),0)
        self.assertEqual(self.l.ingest_many(events),0)
    def test_atomic_conflicting_receipt_keeps_evidence(self):
        e=event('f','funding',amount_usdt='2');self.l.ingest_many([e]);v=self.l.version()
        with self.assertRaises(Conflict):self.l.ingest_many([event('g','funding',amount_usdt='4'),event('f','funding',amount_usdt='3')])
        self.assertEqual(self.l.version(),v);self.assertEqual(self.l.state()['funding_received_usdt'],'2');self.assertEqual(len(self.l.state()['quarantine']),1)
    def test_unit_multiplier_1000_and_fees(self):
        self.l.ingest_many([fill('p','perp','BUY','0.2','2000',fee='0.1')]);st=self.l.state()
        self.assertEqual(D(st['perp_positions']['1000TESTUSDT']['qty']),D('200'));self.assertEqual(D(st['perp_positions']['1000TESTUSDT']['entry_per_token']),D('2'))
        wrong=fill('x','perp','BUY','1','2000');wrong['quantity_token']='1'
        with self.assertRaises(ReconcileError):self.l.ingest_many([wrong])
    def test_buyback_is_not_provider_return(self):
        self.l.ingest_many([fill('s','spot','SELL','100','1',1),fill('b','spot','BUY','100','1.1',2),event('restore','coin_return',3,token='TEST',quantity_token='100',stage='RESTORED_IN_A')])
        self.assertEqual(self.l.state()['provider_outstanding_token']['TEST'],'1000')
        self.l.ingest_many([event('ret','coin_return',4,token='TEST',quantity_token='100',stage='RETURNED_TO_PROVIDER')]);self.assertEqual(self.l.state()['provider_outstanding_token']['TEST'],'900')
    def test_coin_fee_leaves_visible_hedge_gap(self):
        self.l.ingest_many([fill('s','spot','SELL','100','1',fee='1',fee_asset='TEST'),fill('p','perp','BUY','0.1','1000')]);self.assertEqual(D(self.l.state()['hedge_error_token']['1000TESTUSDT']),D('-1'))

class TreasuryTests(unittest.TestCase):
    def score(self,r):return decide(r,r['as_of_ms'])
    def test_E11_unrealized_not_transferable_cash(self):
        r=request();r['account_a']['settled_usdt']='0';r['account_b']['exchange_max_withdraw_usdt']='0';r['account_b']['unrealized_pnl_usdt']='99999999';out=self.score(r)
        self.assertFalse(out['allow_new_exposure']);self.assertEqual(out['account_b']['transferable_settled_usdt'],'0');self.assertTrue(any(x['action']=='WAIT_BUYBACK_CASH' for x in out['intents']))
    def test_E12_recall_lock_survives_stale_quote(self):
        r=request();r['instruments'][0]['risk_events']=['BORROW_RECALL'];r['snapshot_received_at_ms']-=20000;out=self.score(r)
        self.assertTrue(out['instruments'][0]['exit_locked']);self.assertFalse(out['allow_new_exposure']);self.assertEqual(out['execution_actions'],[])
    def test_all_forced_risk_events_lock_exit(self):
        for event in ['ADL','LIQUIDATION','DELISTING','TRADING_HALT','BORROW_RECALL']:
            with self.subTest(event=event):
                r=request();r['instruments'][0]['risk_events']=[event];out=self.score(r);self.assertTrue(out['instruments'][0]['exit_locked']);self.assertFalse(out['allow_new_exposure'])
    def test_regular_sub_needs_master_conversion_route(self):
        r=request();r['account_kind']='REGULAR_SUB';out=self.score(r)
        self.assertIn('SUBACCOUNT_MASTER_CONVERSION_ROUTE_UNVERIFIED',out['reason_codes'])
    def test_E13_tail_obligation_not_marked_closed(self):
        r=request();r['instruments'][0]['sold_spot_qty_token']='0.00001';r['instruments'][0]['perp_long_qty_token']='0.00001';r['instruments'][0]['exit_locked']=True;out=self.score(r)
        self.assertEqual(out['instruments'][0]['strategy_buyback_qty_token'],'0.00001');self.assertFalse(out['instruments'][0]['provider_return_verified'])
    def test_E14_stale_request_rejected(self):
        r=request()
        with self.assertRaises(InputError):decide(r,r['as_of_ms']+60000)
    def test_same_asset_cannot_double_count(self):
        r=request();r['yield_assets'].append(copy.deepcopy(r['yield_assets'][0]))
        with self.assertRaises(InputError):self.score(r)
    def test_other_account_does_not_collateralize_B(self):
        r=request();r['yield_assets'][0]['account']='A';r['account_b']['position_notional_usdt']='3000000';out=self.score(r)
        self.assertEqual(out['account_b']['a_collateral_used_usdt'],'0');self.assertEqual(out['account_b']['one_x_gap_usdt'],'1000000')
    def test_redeeming_asset_neither_cash_nor_collateral(self):
        r=request();r['yield_assets'][0]['status']='REDEEMING';out=self.score(r)
        self.assertEqual(out['account_b']['local_collateral_equity_usdt'],'2000000')
    def test_unknown_permission_pauses_new(self):
        r=request();r['yield_assets'][0]['eligibility_verified']=False;self.assertFalse(self.score(r)['allow_new_exposure'])
    def test_single_asset_mode_cannot_credit_LD(self):
        r=request();r['account_mode']='USDT_SINGLE';self.assertIn('ACCOUNT_MODE_NOT_SUPPORT_YIELD_COLLATERAL',self.score(r)['reason_codes'])
    def test_quote_haircut_and_depeg(self):
        r=request();r['yield_assets'][0]['nav_usdt_per_unit']='0.9';r['yield_assets'][0]['collateral_ratio']='0.95';self.assertEqual(D(self.score(r)['account_b']['local_collateral_equity_usdt']),D('8840000'))
    def test_PM_pro_tiers_apply_incrementally(self):
        row=request()['yield_assets'][0];row['quantity']='8000000';row['tiers']=[{'ceiling_usdt':'3000000','collateral_ratio':'1'},{'ceiling_usdt':'6000000','collateral_ratio':'0.95'},{'ceiling_usdt':'999999999','collateral_ratio':'0.9'}]
        self.assertEqual(product_collateral(row),D('7650000.00'))
    def test_PM_pro_unknown_tiers_pauses(self):
        r=request();r['account_mode']='PM_PRO';self.assertFalse(self.score(r)['allow_new_exposure'])
    def test_submitted_budget_never_exceeds_RC2(self):
        r=request();r['rc2_new_exposure_budget_usdt']='12.34';self.assertEqual(D(self.score(r)['max_new_exposure_usdt']),D('12.34'))
    def test_unknown_field_or_string_boolean_rejected(self):
        r=request();r['allow_margin_magic']=True
        with self.assertRaises(InputError):self.score(r)
        r=request();r['yield_assets'][0]['eligibility_verified']='false'
        with self.assertRaises(InputError):self.score(r)
    def test_decimal_nan_rejected(self):
        r=request();r['account_a']['settled_usdt']='NaN'
        with self.assertRaises(InputError):self.score(r)
    def test_duplicate_json_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x.json';p.write_text('{"x":1,"x":2}')
            with self.assertRaises(ValueError):strict_load(p)
if __name__=='__main__':unittest.main(verbosity=2)
