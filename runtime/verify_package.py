"""Verify the complete offline package, including its canonical profile hash."""
from pathlib import Path
import hashlib,json

ROOT=Path(__file__).resolve().parent
def main():
    expected={}
    for line in (ROOT/'SHA256SUMS.txt').read_text().splitlines():
        h,rel=line.split('  ',1)
        p=ROOT/rel
        if p.resolve().parent!=ROOT.resolve() and ROOT.resolve() not in p.resolve().parents:raise ValueError('path escapes package')
        if rel in expected:raise ValueError('duplicate manifest path')
        expected[rel]=h
        if hashlib.sha256(p.read_bytes()).hexdigest()!=h:raise ValueError('hash mismatch: '+rel)
    actual={str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='SHA256SUMS.txt'}
    if actual!=set(expected):raise ValueError('file inventory mismatch')
    m=json.loads((ROOT/'MODEL_PROFILE.json').read_text())
    raw=json.dumps(m['profile'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    if hashlib.sha256(raw).hexdigest()!=m['profile_sha256']:raise ValueError('canonical profile hash mismatch')
    sources=m['runtime_source_files_sha256']
    source_hash=hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if source_hash!=m['profile']['runtime_source_sha256']:raise ValueError('runtime source map/profile mismatch')
    for rel,wanted in sources.items():
        if hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()!=wanted:raise ValueError('runtime source mismatch: '+rel)
    print('PASS:',len(expected),'files; canonical profile verified')

if __name__=='__main__':main()
