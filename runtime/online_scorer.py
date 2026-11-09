"""Point-in-time r7 income, calibrated cost and public-data ADL proxy. Stdlib only."""
from bisect import bisect_right
from datetime import datetime,timezone
from decimal import Decimal as Q
from math import isfinite,log,sqrt
from statistics import mean,median,stdev
import hashlib,json
from schema_check import require,strict_load

HOUR=3600000
def number(x):
    if x is None or not isfinite(float(x)):return None
    return format(Q(str(float(x))),'f')
def finite_positive(x):return x is not None and isfinite(float(x)) and float(x)>0

class Scorer:
    def __init__(self,document):
        self.bundle=document['bundle'];self.sha=document['bundle_sha256']
        raw=json.dumps(self.bundle,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        require(hashlib.sha256(raw).hexdigest()==self.sha,'model bundle hash mismatch')
        require(self.bundle['format_version']=='1.0.0','unsupported model bundle')
        self.records=sorted(self.bundle['calibration'],key=lambda r:r['available_at_ms'])
        self.times=[r['available_at_ms'] for r in self.records];self.cache={}

    @classmethod
    def from_path(cls,path):return cls(strict_load(path))

    def calibration(self,symbol,now):
        end=bisect_right(self.times,now)
        if end not in self.cache:
            ys=[];hs=[];own_y={};own_h={};last={}
            for r in self.records[:end]:
                s=r['symbol'];p=self.bundle['asset_priors'][s]
                y=r['y_kyle'];h=r['half_spread_perp'];used=False
                if finite_positive(y):ys.append(y);own_y.setdefault(s,[]).append(y);used=True
                if finite_positive(h) and p['half_spread_perp']>0:
                    h=h/p['half_spread_perp'];hs.append(h);own_h.setdefault(s,[]).append(h);used=True
                if used:last[s]=r['available_at_ms']
            self.cache[end]=(ys,hs,own_y,own_h,last)
        ys,hs,own_y,own_h,last=self.cache[end]
        if not ys or not hs:return None
        y=median(ys);h=median(hs)
        if symbol in own_y:y=.5*mean(own_y[symbol][-3:])+.5*y
        if symbol in own_h:h=.5*mean(own_h[symbol][-3:])+.5*h
        # Pool participation itself can affect a coin's shrunken estimate.
        last_available=max(self.times[:end]) if end else None
        return min(3.,max(.1,y)),min(4.,max(.25,h)),last_available

    def estimate(self,r8,basis,now):
        model=next((m for m in self.bundle['income_models'] if m['valid_from_ms']<=now<m['valid_until_ms']),None)
        if model is None:return None,'MODEL_MONTH_UNAVAILABLE'
        fb=max(0,min(3,bisect_right(self.bundle['funding_bucket_edges'],-float(r8))-1))
        bb=bisect_right(self.bundle['basis_bucket_edges'],float(basis))
        joint=model['joint'][fb][bb];f=model['funding'][fb];b=model['basis'][bb]
        if joint['mean'] is not None:
            value=joint['mean'];n=joint['n'];end=joint['max_outcome_end_ms'];source='joint'
        elif f['mean'] is not None and b['mean'] is not None:
            value=f['mean']+b['mean'];n=min(f['n'],b['n']);end=max(f['max_outcome_end_ms'],b['max_outcome_end_ms']);source='marginal_fallback'
        else:return {'model':model,'fb':fb,'bb':bb},'INCOME_SAMPLE_SHORTAGE'
        require(n>=200 and end<model['valid_from_ms'],'invalid model sample cutoff')
        return {'model':model,'fb':fb,'bb':bb,'value':value,'n':n,'end':end,'source':source},None

    def history_features(self,rows,now,replay):
        if not rows:return None,'MODEL_HISTORY_MISSING'
        stamps=[r['bar_end_ms'] for r in rows]
        if stamps!=sorted(set(stamps)):return None,'MODEL_HISTORY_DUPLICATE_OR_UNSORTED'
        if any(r['bar_end_ms']>r['received_at_ms'] or r['received_at_ms']>now or r['bar_end_ms']>now for r in rows):return None,'MODEL_HISTORY_FUTURE'
        if not replay and any(r['availability_source']!='observed' for r in rows):return None,'MODEL_HISTORY_RECEIVE_PROXY'
        end=now//HOUR*HOUR;by={r['bar_end_ms']:r for r in rows}
        expected=[end-j*HOUR for j in range(25,-1,-1)]
        if any(t not in by for t in expected):return None,'MODEL_HISTORY_GAP'
        h=[by[t] for t in expected]
        # Full independent history avoids importing a future/global volatility
        # median when the online market universe differs from the research set.
        prices=[r['spot_close_usdt_per_token'] for r in h[-25:]]
        if not all(finite_positive(p) for p in prices):return None,'VOLATILITY_HISTORY_INCOMPLETE'
        returns=[log(float(b))-log(float(a)) for a,b in zip(prices,prices[1:])]
        sigma=stdev(returns)*sqrt(24)
        adl=False
        for prev,current in zip(h[:-2],h[1:-1]):
            if not all(finite_positive(x) for x in [prev['perp_close_usdt_per_token'],current['perp_close_usdt_per_token'],prev['oi_usdt'],current['oi_usdt']]):return None,'ADL_HISTORY_INCOMPLETE'
            # Match the frozen feature arithmetic and strict comparisons.
            p0,p1=float(prev['perp_close_usdt_per_token']),float(current['perp_close_usdt_per_token'])
            ret=p1/p0-1;change=(float(current['oi_usdt'])/p1)/(float(prev['oi_usdt'])/p0)-1
            available=max(current['bar_end_ms']+HOUR,current['received_at_ms'],prev['received_at_ms'])
            expires=current['bar_end_ms']+25*HOUR
            if ret>.10 and change<0 and available<=now<expires:adl=True
        return {'sigma':sigma,'adl':adl,'last':h[-1]},None

    def score(self,row,now,replay=False):
        out={'status':'UNAVAILABLE','model_id':'frozen-r7-online-v1','observed_at_ms':now,
            'expected_income_rate':None,'round_trip_cost_rate':None,'r7_gate_passed':False,'adl_entry_blocked':False,
            'calibration_id':self.sha,'calibration_is_observed':False,'effective_entry_r8':None,
            'income_training_outcomes_end_ms':None,'income_sample_count':0,
            'income_model_valid_from_ms':None,'income_model_valid_until_ms':None,'income_estimate_source':'unavailable',
            'funding_bucket':None,'basis_bucket':None,'sigma_24h':None,'calibration_y':None,
            'calibration_spread_multiplier':None,'calibration_data_last_available_ms':None,'calibration_age_hours':None,
            'cost_reference_slice_notional_usdt':None,'model_bundle_sha256':self.sha,'runtime_replay':replay}
        if not replay and now<self.bundle['published_at_ms']:return out,['MODEL_NOT_YET_PUBLISHED']
        i,p,m=row['instrument'],row['position'],row['market'];symbol=i['perp_symbol']
        prior=self.bundle['asset_priors'].get(symbol)
        if prior is None:return out,['MODEL_ASSET_UNKNOWN']
        if prior['spot_symbol']!=i['spot_symbol'] or Q(prior['tokens_per_perp_quantity'])!=Q(i['tokens_per_perp_quantity']):return out,['MODEL_INSTRUMENT_MISMATCH']
        if not all(finite_positive(m[x]) for x in ['spot_close_usdt_per_token','perp_close_usdt_per_token','trailing_24h_paired_quote_volume_usdt']) or m['settled_r8'] is None:return out,['MODEL_MARKET_INPUT_MISSING']
        rate=Q(m['settled_r8']);effective=rate
        if rate<0 and Q(p['perp_long_qty_token'])>0:
            if not finite_positive(m['open_interest_usdt']):return out,['MODEL_OI_MISSING']
            effective=min(Q(0),rate+Q('.04')*Q(p['perp_long_qty_token'])*Q(m['perp_close_usdt_per_token'])/Q(m['open_interest_usdt']))
        out['effective_entry_r8']=format(effective,'f')
        basis=(float(m['perp_close_usdt_per_token'])-float(m['spot_close_usdt_per_token']))/float(m['spot_close_usdt_per_token'])
        estimated,error=self.estimate(rate,basis,now)
        if estimated and 'model' in estimated:
            model=estimated['model'];out.update(income_model_valid_from_ms=model['valid_from_ms'],income_model_valid_until_ms=model['valid_until_ms'],funding_bucket=estimated['fb'],basis_bucket=estimated['bb'])
        if error:return out,[error]
        features,error=self.history_features(row.get('model_history'),now,replay)
        if error:return out,[error]
        for key in ['spot_close_usdt_per_token','perp_close_usdt_per_token']:
            if Q(features['last'][key])!=Q(m[key]):return out,['HISTORY_LATEST_PRICE_MISMATCH']
        calibrated=self.calibration(symbol,now)
        if calibrated is None:return out,['CALIBRATION_UNAVAILABLE']
        y,h,last=calibrated;dv=float(m['trailing_24h_paired_quote_volume_usdt']);qref=min(.03*dv/24,1e7)
        cost=.003+4*y*features['sigma']*sqrt(qref/max(dv,1))+2*(prior['half_spread_spot']+prior['half_spread_perp'])*h
        income=number(estimated['value']);cost_str=number(cost)
        out.update(status='VALID',expected_income_rate=income,round_trip_cost_rate=cost_str,
            r7_gate_passed=Q(income)>=2*Q(cost_str),adl_entry_blocked=features['adl'],calibration_is_observed=True,
            income_training_outcomes_end_ms=estimated['end'],income_sample_count=estimated['n'],income_estimate_source=estimated['source'],
            sigma_24h=number(features['sigma']),calibration_y=number(y),calibration_spread_multiplier=number(h),
            calibration_data_last_available_ms=last,calibration_age_hours=number((now-last)/HOUR),cost_reference_slice_notional_usdt=number(qref))
        reasons=[]
        latest_day=datetime.fromtimestamp((last-48*HOUR)/1000,tz=timezone.utc).strftime('%Y-%m-%d')
        if latest_day[:7]!=datetime.fromtimestamp(now/1000,tz=timezone.utc).strftime('%Y-%m'):reasons.append('COST_CALIBRATION_FROM_PRIOR_MONTH')
        return out,reasons
