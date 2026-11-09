import sys,unittest,copy
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from execution_analytics import analyze
class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.report={'fills':[{'account':'B','market':'perp','symbol':'1000TESTUSDT','trade_id':'1','side':'BUY','quantity_exchange':'2','tokens_per_exchange_quantity':'1000','quantity_token':'2000','price_usdt_per_exchange_quantity':'1010','fee_asset':'USDT','fee_amount':'1','event_time_ms':1000}]}
        self.b={'source_mode':'synthetic','rows':[{'account':'B','market':'perp','symbol':'1000TESTUSDT','trade_id':'1','quote_event_time_ms':10,'quote_received_at_ms':11,'order_submit_time_ms':12,'bid_usdt_per_exchange_quantity':'990','ask_usdt_per_exchange_quantity':'1010','fee_asset_usdt_price':None}]}
    def test_independent_literal_1000_contract_shortfall(self):
        out=analyze(self.report,self.b)['rows'][0];self.assertEqual(D(out['shortfall_usdt']),D(21));self.assertEqual(D(out['shortfall_bps']),D(105))
    def test_sell_sign_and_price_improvement(self):
        self.report['fills'][0]['side']='SELL';self.report['fills'][0]['price_usdt_per_exchange_quantity']='1010';out=analyze(self.report,self.b)['rows'][0];self.assertEqual(D(out['shortfall_usdt']),D(-19))
    def test_future_quote_not_a_benchmark(self):
        self.b['rows'][0]['quote_received_at_ms']=13;self.assertEqual(analyze(self.report,self.b)['rows'][0]['status'],'INVALID_INFORMATION_TIMING')
    def test_unknown_fee_not_silently_zero(self):
        self.report['fills'][0]['fee_asset']='BNB';out=analyze(self.report,self.b);self.assertIsNone(out['rows'][0]['shortfall_usdt']);self.assertIsNone(out['funding_impact_k_estimate'])
    def test_missing_quote_keeps_missing_record(self):
        self.b['rows']=[];self.assertEqual(analyze(self.report,self.b)['rows'][0]['status'],'MISSING_PREORDER_BENCHMARK')
if __name__=='__main__':unittest.main(verbosity=2)
