"""Rebuild November's labels with the dataset physically cut at Nov 1."""
import json,types,sys
import numpy as np
from bootstrap import load,ROOT

def main():
    D,R7,RR,RA,RS,manifest=load();M=RA.HABS('2026-11-01');N=D.N
    namespace=dict(RA.sample_outcomes.__globals__)
    namespace.update(T=M,N=N,sum24=RS.sum24[:M],cev=np.cumsum(RS.ev[:M],axis=0))
    only_past=types.FunctionType(RA.sample_outcomes.__code__,namespace)
    funding,end,complete=only_past(24)
    bnow=(D.arr[4][:M]-D.arr[1][:M])/np.where(D.arr[1][:M]>0,D.arr[1][:M],np.nan)
    db=np.take_along_axis(bnow,np.minimum(end,M-1),axis=0)-RA.b_prev[:M]
    lo=max(0,M-90*24);sel=RA.SIG[lo:M]&complete[lo:M]&(end[lo:M]<M)&np.isfinite(db[lo:M])
    bundle=json.loads((ROOT/'runtime/models/r7-model-bundle.json').read_text())['bundle']
    cutoff=RA.T0.value//1000000+M*3600000
    target=next(m for m in bundle['income_models'] if m['valid_from_ms']==cutoff)
    errors=[];comparisons=0
    for typ,values,count,axis in [('funding',funding,4,RA.BKT),('basis',db,3,R7.BBKT)]:
        for k in range(count):
            mask=sel&(axis[lo:M]==k);item=target[typ][k];assert item['n']==int(mask.sum())
            got=float(values[lo:M][mask].mean()) if mask.sum()>=200 else None
            assert (got is None)==(item['mean'] is None)
            if got is not None:errors.append(abs(got-item['mean']))
            comparisons+=1
    for j in range(4):
        for k in range(3):
            mask=sel&(RA.BKT[lo:M]==j)&(R7.BBKT[lo:M]==k);item=target['joint'][j][k]
            assert item['n']==int(mask.sum());got=float((funding[lo:M]+db[lo:M])[mask].mean()) if mask.sum()>=200 else None
            assert (got is None)==(item['mean'] is None)
            if got is not None:errors.append(abs(got-item['mean']))
            comparisons+=1
    assert max(errors,default=0)<1e-12
    out={'status':'PASS','new_model_month':'2026-11','future_data_removed_from':'2026-11-01T00:00:00Z',
         'bucket_statistics_compared':comparisons,'maximum_mean_difference':max(errors,default=0),
         'sample_counts_identical':True,'online_model_retrained':False}
    (ROOT/'audit/NOVEMBER_PREFIX_VALIDATION.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out))

if __name__=='__main__':main()
