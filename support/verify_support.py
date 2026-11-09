"""Verify the support package's original bytes; no network access."""
import hashlib,json
from pathlib import Path
def main():
    root=Path(__file__).resolve().parent;manifest=json.loads((root/'SUPPORT_SHA256.json').read_text());bad=[]
    for name,expected in manifest.items():
        p=root/name
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:bad.append(name)
    if bad:raise SystemExit('FAIL: '+','.join(bad))
    print(f'PASS: {len(manifest)} support files; no execution-side acceptance implied')
if __name__=='__main__':main()
