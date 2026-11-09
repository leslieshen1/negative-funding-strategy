"""SQLite reference event ledger for offline acceptance; no exchange or order submission API."""
from decimal import Decimal as D
from pathlib import Path
import copy, hashlib, json, sqlite3
from treasury_risk import money, s, InputError
from schema_check import shape

class Conflict(ValueError):pass
class ReconcileError(ValueError):pass
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()

class Ledger:
    def __init__(self,path):
        self.path=str(path); self.db=sqlite3.connect(self.path,timeout=20,isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA busy_timeout=20000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        INSERT OR IGNORE INTO meta VALUES('version','0');
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,hash TEXT NOT NULL,result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY,hash TEXT NOT NULL,payload TEXT NOT NULL,event_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries(event_key TEXT NOT NULL,received_ms INTEGER NOT NULL,raw_hash TEXT NOT NULL,PRIMARY KEY(event_key,received_ms,raw_hash));
        CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,hash TEXT NOT NULL,symbol TEXT NOT NULL,hour INTEGER NOT NULL,total TEXT NOT NULL,filled TEXT NOT NULL,active TEXT NOT NULL,expires INTEGER NOT NULL,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS consumptions(id TEXT PRIMARY KEY,decision_id TEXT NOT NULL,amount TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(version INTEGER PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quarantine(id TEXT PRIMARY KEY,payload TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL);
        ''')
    def close(self):self.db.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def version(self):return int(self.db.execute("SELECT v FROM meta WHERE k='version'").fetchone()[0])
    def _increment(self):self.db.execute("UPDATE meta SET v=CAST(v AS INTEGER)+1 WHERE k='version'")
    def set_opening(self,opening):
        text=canonical(opening); existing=self.db.execute("SELECT v FROM meta WHERE k='opening'").fetchone()
        if existing and existing[0]!=text:raise Conflict('Opening ledger cannot be overwritten')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('opening',?)",(text,))
    def cache_request(self,request_id,payload,result):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT hash,result FROM requests WHERE id=?',(request_id,)).fetchone()
            if old:
                if old[0]!=digest(payload):raise Conflict('REQUEST_ID_CONTENT_CONFLICT')
                self.db.execute('COMMIT'); return json.loads(old[1]),False
            self.db.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,digest(payload),canonical(result)))
            self.db.execute('COMMIT');return result,True
        except BaseException:self.db.execute('ROLLBACK');raise
    def reserve(self,decision_id,payload,expected_version,symbol,hour,amount,total_cap,coin_cap,hour_cap,expires_ms):
        amount=money(amount); caps=list(map(money,[total_cap,coin_cap,hour_cap]))
        if amount<=0:raise Conflict('Reservation must be positive')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT hash FROM reservations WHERE id=?',(decision_id,)).fetchone()
            if old:
                if old[0]!=digest(payload):raise Conflict('DECISION_CONTENT_CONFLICT')
                self.db.execute('COMMIT');return False
            if self.version()!=expected_version:raise Conflict('STALE_LEDGER_VERSION')
            active=self.db.execute('SELECT symbol,hour,active FROM reservations').fetchall()
            totals=[sum((D(x[2]) for x in active),D(0)),sum((D(x[2]) for x in active if x[0]==symbol),D(0)),sum((D(x[2]) for x in active if x[1]==hour),D(0))]
            if any(t+amount>cap for t,cap in zip(totals,caps)):raise Conflict('SHARED_BUDGET_EXCEEDED')
            self.db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)',(decision_id,digest(payload),symbol,hour,s(amount),'0',s(amount),expires_ms,'OPEN'))
            self._increment();self.db.execute('COMMIT');return True
        except BaseException as exc:
            self.db.execute('ROLLBACK')
            raise
    def cancel(self,decision_id,confirmed=False):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.db.execute('SELECT id FROM reservations WHERE id=?',(decision_id,)).fetchone():raise Conflict('UNKNOWN_DECISION')
            if confirmed:self.db.execute("UPDATE reservations SET active=filled,status='CANCELLED' WHERE id=?",(decision_id,))
            else:self.db.execute("UPDATE reservations SET status='CANCEL_PENDING' WHERE id=?",(decision_id,))
            self._increment();self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def consume(self,decision_id,amount,now_ms,consumption_id):
        """Consume new permission, not a late fill receipt. Receipts use ingest even after expiry."""
        amount=money(amount)
        if amount<=0:raise Conflict('Consumption must be positive')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior=self.db.execute('SELECT decision_id,amount FROM consumptions WHERE id=?',(consumption_id,)).fetchone()
            if prior:
                if prior!=(decision_id,s(amount)):raise Conflict('CONSUMPTION_ID_CONTENT_CONFLICT')
                self.db.execute('COMMIT');return False
            row=self.db.execute('SELECT total,filled,active,expires,status FROM reservations WHERE id=?',(decision_id,)).fetchone()
            if row is None:raise Conflict('UNKNOWN_DECISION')
            if now_ms>=row[3] or row[4]!='OPEN':raise Conflict('EXPIRED_OR_CANCELLED_PERMISSION')
            if amount>D(row[0])-D(row[1]):raise Conflict('OVERCONSUMPTION')
            # Consumed/filled risk remains reserved until the execution-side position budget replaces it.
            self.db.execute('UPDATE reservations SET filled=? WHERE id=?',(s(D(row[1])+amount),decision_id))
            self.db.execute('INSERT INTO consumptions VALUES(?,?,?)',(consumption_id,decision_id,s(amount)));self._increment();self.db.execute('COMMIT');return True
        except BaseException:self.db.execute('ROLLBACK');raise
    def ingest_many(self,events):
        self.db.execute('BEGIN IMMEDIATE'); inserted=0
        try:
            for event in events:
                if event.get('schema_version')!='nfb.ledger-event.v1':raise InputError('Unknown event schema')
                event_ms=event['event_time_ms']; received=event['received_at_ms']
                if type(event_ms)!=int or type(received)!=int or event_ms<0 or received<event_ms:raise InputError('Invalid event/receipt timestamp')
                key=event['event_key']; text=canonical(event)
                semantic={k:v for k,v in event.items() if k!='received_at_ms'}
                if event.get('type')=='transfer' and event.get('status')=='PENDING':semantic.pop('event_time_ms',None)
                h=digest(semantic)
                old=self.db.execute('SELECT hash FROM events WHERE key=?',(key,)).fetchone()
                if old:
                    if old[0]!=h:raise Conflict('EVENT_KEY_CONTENT_CONFLICT:'+key)
                    self.db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',(key,event['received_at_ms'],digest(event)))
                    continue
                self.db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(key,h,text,event_ms,received));inserted+=1
                self.db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',(key,received,digest(event)))
            if inserted:self._increment()
            # Invalid units, impossible repayment, or an unsupported event rolls back the entire report.
            self._state()
            self.db.execute('COMMIT'); return inserted
        except BaseException as exc:
            self.db.execute('ROLLBACK')
            text=canonical(events)
            self.db.execute('INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?)',(digest(events),text,str(exc),'PENDING_RECONCILIATION'))
            raise
    def retry_quarantine(self):
        result=[]
        for ident,text,_ in self.db.execute("SELECT id,payload,reason FROM quarantine WHERE status='PENDING_RECONCILIATION'").fetchall():
            try:
                inserted=self.ingest_many(json.loads(text));self.db.execute("UPDATE quarantine SET status='RECOVERED' WHERE id=?",(ident,));result.append({'id':ident,'inserted':inserted,'recovered':True})
            except (ValueError,KeyError) as exc:result.append({'id':ident,'recovered':False,'reason':str(exc)})
        return result
    def accept_snapshot(self,version,payload):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            latest=self.db.execute('SELECT MAX(version) FROM snapshots').fetchone()[0]
            if latest is not None and version<latest:raise Conflict('LATE_SNAPSHOT_CANNOT_OVERWRITE')
            old=self.db.execute('SELECT payload FROM snapshots WHERE version=?',(version,)).fetchone();text=canonical(payload)
            if old and old[0]!=text:raise Conflict('SNAPSHOT_VERSION_CONFLICT')
            self.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?)',(version,text));self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def state(self):
        self.db.execute('BEGIN')
        try:out=self._state();self.db.execute('COMMIT');return out
        except BaseException:self.db.execute('ROLLBACK');raise
    def _state(self):
        opening=self.db.execute("SELECT v FROM meta WHERE k='opening'").fetchone()
        if not opening:raise ReconcileError('Opening balances and instrument mapping are required')
        opening=json.loads(opening[0]); instruments=opening['instruments']
        cash={k:D(v) for k,v in opening['cash_usdt'].items()}
        owned={k:D(v) for k,v in opening.get('owned_token_A',{}).items()}
        debt={k:D(v) for k,v in opening.get('borrowed_token',{}).items()}
        if len({x['token'] for x in instruments.values()})!=len(instruments):raise ReconcileError('Multiple simultaneous instruments per token need an explicit allocation ledger')
        positions={iid:{'qty':money(p['qty_token']),'entry_per_token':money(p['entry_usdt_per_token'])} for iid,p in opening.get('perp_positions',{}).items()}
        if any(iid not in instruments or p['qty']<0 or p['entry_per_token']<0 for iid,p in positions.items()):raise ReconcileError('Invalid opening perpetual position')
        transferred={}; fees={}; funding=D(0); realized=D(0); restored={}; locks=set(opening.get('exit_locked',[])); late=[]
        rows=self.db.execute('SELECT payload FROM events ORDER BY event_ms,key').fetchall()
        for item in rows:
            e=json.loads(item[0]); kind=e['type']
            if kind=='fill':
                iid=e['instrument_id']
                if iid not in instruments:raise ReconcileError('UNKNOWN_INSTRUMENT')
                spec=instruments[iid]; token=spec['token']; qty=money(e['quantity_exchange']); mult=money(e['tokens_per_exchange_quantity']); qt=money(e['quantity_token']); price=money(e['price_usdt_per_exchange_quantity'])
                if qty<=0 or mult<=0 or qt!=qty*mult or price<=0:raise ReconcileError('UNIT_OR_PRICE_ERROR')
                expected_mult=D(1) if e['market']=='spot' else money(spec['tokens_per_perp_quantity'])
                if mult!=expected_mult:raise ReconcileError('CONTRACT_MULTIPLIER_MISMATCH')
                sign=D(1) if e['side']=='BUY' else D(-1) if e['side']=='SELL' else None
                if sign is None:raise ReconcileError('UNKNOWN_SIDE')
                if e['market']=='spot':
                    if e['account']!='A':raise ReconcileError('WRONG_SPOT_ACCOUNT')
                    cash['A']-=sign*qty*price; owned[token]=owned.get(token,D(0))+sign*qt
                elif e['market']=='perp':
                    if e['account']!='B':raise ReconcileError('WRONG_PERP_ACCOUNT')
                    pos=positions.setdefault(iid,{'qty':D(0),'entry_per_token':D(0)})
                    pt=price/mult
                    if sign>0:
                        pos['entry_per_token']=(pos['entry_per_token']*pos['qty']+qt*pt)/(pos['qty']+qt); pos['qty']+=qt
                    else:
                        if qt>pos['qty']:raise ReconcileError('PERP_REDUCE_EXCEEDS_LONG')
                        pnl=qt*(pt-pos['entry_per_token']);cash['B']+=pnl;realized+=pnl;pos['qty']-=qt
                else:raise ReconcileError('UNKNOWN_MARKET')
                fee=money(e['fee_amount']);fa=e['fee_asset']
                if fee<0:raise ReconcileError('NEGATIVE_FEE_REQUIRES_EXPLICIT_REBATE_EVENT')
                fees[fa]=fees.get(fa,D(0))+fee
                if fa=='USDT':cash[e['account']]-=fee
                elif e['account']=='A' and fa==token:owned[token]=owned.get(token,D(0))-fee
                else:late.append('UNPRICED_FEE_ASSET:'+fa)
                if e.get('permission_expired_at_ms') is not None and e['received_at_ms']>=e['permission_expired_at_ms']:late.append('LATE_FILL_RECORDED:'+e['event_key'])
            elif kind=='funding':
                amount=money(e['amount_usdt']);cash['B']+=amount;funding+=amount
            elif kind=='transfer':
                tid=e['request_id'];amount=money(e['amount']);src=e['from_account'];dst=e['to_account'];status=e['status']
                if amount<=0 or src==dst or {src,dst}!={'A','B'}:raise ReconcileError('INVALID_TRANSFER')
                tr=transferred.setdefault(tid,{'amount':amount,'src':src,'dst':dst,'debited':False,'settled':False,'refunded':False,'failed':False})
                if (tr['amount'],tr['src'],tr['dst'])!=(amount,src,dst):raise ReconcileError('TRANSFER_IDENTITY_CONFLICT')
                if status=='PENDING':pass
                elif status=='DEBITED':
                    if not tr['debited']:cash[src]-=amount;tr['debited']=True
                elif status=='SETTLED':
                    if tr['refunded']:raise ReconcileError('SETTLED_AFTER_REFUND')
                    if not tr['debited']:cash[src]-=amount;tr['debited']=True
                    if not tr['settled']:cash[dst]+=amount;tr['settled']=True
                elif status=='FAILED':tr['failed']=True
                elif status=='REFUNDED':
                    if tr['settled'] or not tr['debited']:raise ReconcileError('INVALID_REFUND')
                    if not tr['refunded']:cash[src]+=amount;tr['refunded']=True
                else:raise ReconcileError('UNKNOWN_TRANSFER_STATUS')
            elif kind=='coin_return':
                token=e['token'];qt=money(e['quantity_token'])
                if qt<=0:raise ReconcileError('INVALID_RETURN_QUANTITY')
                if e['stage']=='RESTORED_IN_A':
                    if qt>owned.get(token,D(0)):raise ReconcileError('RESTORED_MARKER_WITHOUT_COIN_BALANCE')
                    restored[token]=restored.get(token,D(0))+qt
                elif e['stage']=='RETURNED_TO_PROVIDER':
                    if qt>owned.get(token,D(0)) or qt>debt.get(token,D(0)):raise ReconcileError('RETURN_EXCEEDS_COIN_OR_OBLIGATION')
                    owned[token]-=qt;debt[token]-=qt
                else:raise ReconcileError('UNKNOWN_RETURN_STAGE')
            elif kind=='risk':locks.add(e['instrument_id'])
            elif kind=='borrow_receipt':
                token=e['token'];qt=money(e['quantity_token'])
                if qt<=0:raise ReconcileError('INVALID_BORROW_RECEIPT')
                owned[token]=owned.get(token,D(0))+qt;debt[token]=debt.get(token,D(0))+qt
            else:raise ReconcileError('UNKNOWN_EVENT_TYPE')
        hedge={}
        for iid,spec in instruments.items():
            token=spec['token'];p=positions.get(iid,{'qty':D(0),'entry_per_token':D(0)})
            hedge[iid]=s(owned.get(token,D(0))+p['qty']-debt.get(token,D(0)))
        pending=sum((tr['amount'] for tr in transferred.values() if tr['debited'] and not tr['settled'] and not tr['refunded']),D(0))
        quarantine=[{'id':i,'reason':reason,'status':status} for i,reason,status in self.db.execute('SELECT id,reason,status FROM quarantine').fetchall()]
        return {'schema_version':'nfb.ledger-state.v1','ledger_version':self.version(),'cash_usdt':{k:s(v) for k,v in cash.items()},'owned_token_A':{k:s(v) for k,v in owned.items()},'provider_outstanding_token':{k:s(v) for k,v in debt.items()},'perp_positions':{k:{a:s(v) for a,v in p.items()} for k,p in positions.items()},'hedge_error_token':hedge,'funding_received_usdt':s(funding),'realized_perp_pnl_usdt':s(realized),'fees_by_asset':{k:s(v) for k,v in fees.items()},'internal_transfer_in_flight_usdt':s(pending),'exit_locked':sorted(locks),'warnings':sorted(set(late)),'quarantine':quarantine,'event_count':len(rows),'real_execution_verified':False}

def report_events(report,mapping):
    """Normalize RC2 report components; event keys survive report-id changes."""
    shape(report,json.loads((Path(__file__).parent/'schemas/execution-report.schema.json').read_text()))
    if report['protocol_version']!='2.1.0' or report['message_type']!='execution_report':raise InputError('Not a RC2 execution report')
    expected=json.loads((Path(__file__).parent/'frozen_identity.json').read_text())
    if report['model']!=expected:raise InputError('RC2 model identity mismatch')
    out=[]
    def add(key,kind,ev,received,fields):out.append(dict(schema_version='nfb.ledger-event.v1',event_key=key,type=kind,event_time_ms=ev,received_at_ms=received,**fields))
    for fill in report['fills']:
        key=(fill['account'],fill['market'],fill['symbol'])
        if key not in mapping:raise InputError('Instrument mapping missing: '+str(key))
        fields={k:v for k,v in fill.items() if k not in ['trade_id','client_order_id','symbol','event_time_ms','received_at_ms']};fields['instrument_id']=mapping[key]
        add('fill:'+':'.join(key)+':'+fill['trade_id'],'fill',fill['event_time_ms'],fill['received_at_ms'],fields)
    for f in report['funding_receipts']:
        add('funding:'+f['perp_symbol']+':'+f['event_id'],'funding',f['funding_time_ms'],f['received_at_ms'],{'amount_usdt':f['amount_usdt']})
    for tr in report['transfer_receipts']:
        if tr['asset']!='USDT':raise InputError('Non-USDT transfer needs an explicit currency ledger')
        ev=tr['settled_at_ms'] if tr['status']=='SETTLED' else report['as_of_ms']
        if ev is None:raise InputError('Missing actual transfer arrival time')
        add('transfer:'+tr['request_id']+':'+tr['status'],'transfer',ev,report['as_of_ms'],{k:tr[k] for k in ['request_id','status','from_account','to_account','amount']})
    for c in report['coin_return_receipts']:
        add('coin_return:'+c['receipt_id']+':'+c['stage'],'coin_return',c['event_time_ms'],c['received_at_ms'],{k:c[k] for k in ['token','quantity_token','stage']})
    return out
