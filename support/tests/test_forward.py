import tempfile,unittest,sys,copy
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from forward_log import ForwardLog

class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.log=ForwardLog(Path(self.tmp.name)/'forward.sqlite');self.at=datetime(2026,11,10,tzinfo=timezone.utc)
        self.request={'request_id':'p1','asof_utc':self.at.isoformat(),'symbol':'TESTUSDT','information_mode':'observed_asof_snapshot'}
        self.prediction={**{k:self.request[k] for k in ['request_id','asof_utc','symbol']},'model_version':'nfb.inventory-value.v3.20261109','submission_allowed':False,'research_only':True,'data_quality':{'snapshot_stale':False,'model_stale':False}}
        self.evidence={'source_file_hashes':{'raw':'a'*64},'model_file_hashes':{'model':'b'*64},'received_at_utc':self.at.isoformat()}
    def tearDown(self):self.log.close();self.tmp.cleanup()
    def record(self):self.log.record(self.request,self.prediction,self.evidence,now=self.at,synthetic_test=True)
    def obs(self):return {'prediction_id':'p1','target':'p_new_negative_within72h','coverage_start_utc':self.at.isoformat(),'coverage_end_exclusive_utc':(self.at+timedelta(hours=72)).isoformat(),'source_file_hashes':{'settlements':'c'*64},'coverage_complete':True,'late_data':False,'censored':False,'label_basis':'frozen_v3_target_definition','outcome':True,'first_event_utc':(self.at+timedelta(hours=12)).isoformat()}
    def test_no_backdating(self):
        with self.assertRaises(ValueError):self.log.record(self.request,self.prediction,self.evidence,now=self.at+timedelta(hours=1))
        self.assertEqual(self.log.health()['recorded_predictions'],0)
    def test_prediction_id_cannot_be_rewritten(self):
        self.record();p=copy.deepcopy(self.prediction);p['score']=.9
        with self.assertRaises(ValueError):self.log.record(self.request,p,self.evidence,now=self.at,synthetic_test=True)
    def test_cannot_mature_early(self):
        self.record()
        with self.assertRaises(ValueError):self.log.outcome(self.obs(),now=self.at+timedelta(hours=71))
    def test_missing_label_not_false_negative(self):
        self.record();o=self.obs();o['coverage_complete']=False;self.log.outcome(o,now=self.at+timedelta(hours=73))
        self.assertEqual(self.log.health()['missing_late_censored'],1);self.assertIsNone(self.log.health()['prospective_hit_rate'])
    def test_synthetic_records_not_real_predictions(self):
        self.record();self.assertEqual(self.log.health()['observed_non_synthetic_predictions'],0)
    def test_label_window_cannot_change(self):
        self.record();o=self.obs();o['coverage_end_exclusive_utc']=(self.at+timedelta(hours=73)).isoformat()
        with self.assertRaises(ValueError):self.log.outcome(o,now=self.at+timedelta(hours=74))
    def test_late_data_revision_preserves_original_missing_label(self):
        self.record();first=self.obs();first['coverage_complete']=False;self.log.outcome(first,now=self.at+timedelta(hours=73))
        later=self.obs()
        with self.assertRaises(ValueError):self.log.outcome(later,now=self.at+timedelta(hours=74))
        later['correction_reason']='Additional independently sourced settlements arrived';self.log.outcome(later,now=self.at+timedelta(hours=74))
        health=self.log.health();self.assertEqual(health['supplied_outcomes'],1);self.assertEqual(health['supplied_outcome_revisions'],2);self.assertEqual(health['missing_late_censored'],1);self.assertEqual(health['independently_verified_outcomes'],0)
if __name__=='__main__':unittest.main(verbosity=2)
