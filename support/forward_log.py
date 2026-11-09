"""Manual append-only v3 observation log. Never schedules work, trains, or changes old ledgers."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import sqlite3,json,hashlib,argparse,subprocess,sys
from schema_check import strict_load

WINDOWS={'p_new_negative_within72h':(0,72),'p_no_raw24_exit_next168h':(0,168),'p_qualified_entry_after72h_within7d':(72,240),'conditional_30d_reference_net_usdt':(72,1008)}
def stamp(x):
    t=datetime.fromisoformat(x.replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('Timezone required')
    return t.astimezone(timezone.utc)
def canon(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False,ensure_ascii=False)
def digest(x):return hashlib.sha256(canon(x).encode()).hexdigest()

class ForwardLog:
    def __init__(self,path):
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at_utc TEXT,status TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY,hash TEXT NOT NULL,asof TEXT NOT NULL,issued TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outcomes(id TEXT NOT NULL,target TEXT NOT NULL,hash TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,target));
        CREATE TABLE IF NOT EXISTS outcome_revisions(id TEXT NOT NULL,target TEXT NOT NULL,hash TEXT NOT NULL,payload TEXT NOT NULL,logged_at TEXT NOT NULL,PRIMARY KEY(id,target,hash));''')
    def close(self):self.db.close()
    def record(self,request,prediction,evidence,now=None,synthetic_test=False):
        now=now or datetime.now(timezone.utc); payload={'request':request,'prediction':prediction,'evidence':evidence,'synthetic_test':synthetic_test}
        try:
            asof=stamp(prediction['asof_utc'])
            if request['asof_utc']!=prediction['asof_utc'] or request['symbol']!=prediction['symbol'] or request['request_id']!=prediction['request_id']:raise ValueError('Prediction does not bind the input')
            if (asof.hour,asof.minute,asof.second,asof.microsecond)!=(0,0,0,0):raise ValueError('Frozen daily00UTC grid required')
            if not asof<=now<asof+timedelta(minutes=5):raise ValueError('MISSED_FIXED_GRID_NO_BACKDATED_PREDICTION')
            if prediction['model_version']!='nfb.inventory-value.v3.20261109':raise ValueError('Unexpected frozen candidate')
            if prediction['submission_allowed'] is not False or prediction['research_only'] is not True:raise ValueError('Research-only output required')
            quality=prediction['data_quality']
            if quality['snapshot_stale'] or quality['model_stale']:raise ValueError('Stale snapshot/model')
            if request['information_mode']!='observed_asof_snapshot' and not synthetic_test:raise ValueError('Prospective record requires observed snapshot')
            if not evidence.get('source_file_hashes') or not evidence.get('model_file_hashes'):raise ValueError('Raw snapshot and frozen model hashes required')
            if stamp(evidence['received_at_utc'])>now:raise ValueError('Future evidence receive time')
            ident=prediction['request_id'];h=digest(payload);old=self.db.execute('SELECT hash FROM predictions WHERE id=?',(ident,)).fetchone()
            if old:
                if old[0]!=h:raise ValueError('PREDICTION_ID_CONFLICT')
                return False
            self.db.execute('INSERT INTO predictions VALUES(?,?,?,?,?)',(ident,h,asof.isoformat(),now.isoformat(),canon(payload)))
            self.db.execute('INSERT INTO audit(at_utc,status,payload) VALUES(?,?,?)',(now.isoformat(),'RECORDED_SYNTHETIC' if synthetic_test else 'RECORDED',canon(payload)))
            return True
        except (ValueError,KeyError) as exc:
            self.db.execute('INSERT INTO audit(at_utc,status,payload) VALUES(?,?,?)',(now.isoformat(),'REJECTED:'+str(exc),canon(payload)))
            raise
    def outcome(self,observation,now=None):
        now=now or datetime.now(timezone.utc);ident=observation['prediction_id'];target=observation['target']
        record=self.db.execute('SELECT asof,issued,payload FROM predictions WHERE id=?',(ident,)).fetchone()
        if not record or target not in WINDOWS:raise ValueError('Unknown prediction/target')
        asof,issued=map(stamp,record[:2]);start,end=WINDOWS[target]
        if now<asof+timedelta(hours=end):raise ValueError('FIXED_WINDOW_NOT_MATURE')
        if stamp(observation['coverage_start_utc'])!=asof+timedelta(hours=start) or stamp(observation['coverage_end_exclusive_utc'])!=asof+timedelta(hours=end):raise ValueError('Fixed window mismatch')
        if not observation.get('source_file_hashes'):raise ValueError('Outcome source evidence required')
        if observation['coverage_complete'] is not True or observation['late_data'] is True or observation['censored'] is True:
            observation=dict(observation,outcome=None,evaluation_status='MISSING_LATE_OR_CENSORED')
        elif observation.get('first_event_utc') and stamp(observation['first_event_utc'])<issued:
            observation=dict(observation,outcome=None,evaluation_status='EVENT_ALREADY_OCCURRED_BEFORE_ISSUE')
        else:
            if observation['label_basis']!='frozen_v3_target_definition':raise ValueError('Changed label definition')
            # This is a supplied label, not a fabricated exchange observation or an independent recomputation.
            observation=dict(observation,evaluation_status='SUPPLIED_LABEL_PENDING_INDEPENDENT_RECOMPUTATION')
        h=digest(observation);old=self.db.execute('SELECT hash FROM outcome_revisions WHERE id=? AND target=? ORDER BY rowid DESC LIMIT 1',(ident,target)).fetchone()
        if old:
            if old[0]==h:return False
            if not observation.get('correction_reason'):raise ValueError('OUTCOME_CORRECTION_REQUIRES_EXPLICIT_REASON')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?)',(ident,target,h,canon(observation)))
            self.db.execute('INSERT INTO outcome_revisions VALUES(?,?,?,?,?)',(ident,target,h,canon(observation),now.isoformat()));self.db.execute('COMMIT');return True
        except BaseException:self.db.execute('ROLLBACK');raise
    def health(self):
        count=self.db.execute('SELECT COUNT(*) FROM predictions').fetchone()[0];outcomes=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM outcome_revisions').fetchall()]
        unique_outcomes=self.db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
        real=sum(not json.loads(r[0])['synthetic_test'] for r in self.db.execute('SELECT payload FROM predictions').fetchall())
        return {'schema_version':'nfb.forward-health.v1','recorded_predictions':count,'observed_non_synthetic_predictions':real,'supplied_outcomes':unique_outcomes,'supplied_outcome_revisions':len(outcomes),'independently_verified_outcomes':0,'missing_late_censored':sum(r['evaluation_status']=='MISSING_LATE_OR_CENSORED' for r in outcomes),'automation_enabled':False,'fixed_windows_hours':WINDOWS,'prospective_hit_rate':None}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--database',required=True);ap.add_argument('--request');ap.add_argument('--prediction');ap.add_argument('--evidence');ap.add_argument('--outcome');ap.add_argument('--candidate-directory');ap.add_argument('--scorer-python',default=sys.executable);args=ap.parse_args();log=ForwardLog(args.database)
    try:
        if args.request:
            if not args.candidate_directory:raise ValueError('Frozen v3 candidate directory required for independent score verification')
            root=Path(args.candidate_directory);binding=strict_load(Path(__file__).parent/'v3_binding.json')
            for relative,expected in binding.items():
                if hashlib.sha256((root/relative).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen v3 file hash mismatch: '+relative)
            request=strict_load(args.request);prediction=strict_load(args.prediction);evidence=strict_load(args.evidence)
            for file,expected in evidence['source_file_hashes'].items():
                if hashlib.sha256(Path(file).read_bytes()).hexdigest()!=expected:raise ValueError('Raw evidence hash mismatch')
            raw=subprocess.run([args.scorer_python,'-B',str(root/'src/signal_cli.py'),str(Path(args.request).resolve())],capture_output=True,text=True,check=True)
            recomputed=json.loads(raw.stdout)
            for key in ['request_id','symbol','asof_utc','forecasts','model_version','fit_month','economic_training','event_training','data_quality','valid_until_utc','status']:
                if prediction[key]!=recomputed[key]:raise ValueError('Supplied prediction differs from frozen v3 recomputation: '+key)
            log.record(request,prediction,evidence)
        if args.outcome:log.outcome(strict_load(args.outcome))
        print(json.dumps(log.health(),ensure_ascii=False,indent=2))
    finally:log.close()
if __name__=='__main__':main()
