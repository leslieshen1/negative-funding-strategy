from pathlib import Path
import hashlib,json
ROOT=Path(__file__).resolve().parents[1]
def verify_manifest():
 manifest=json.loads((ROOT/'RELEASE_MANIFEST.json').read_text());bad=[]
 for rel,wanted in manifest['files'].items():
  path=(ROOT/rel).resolve()
  if ROOT.resolve() not in path.parents:raise ValueError('Release path escapes repository')
  if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=wanted:bad.append(rel)
 if bad:raise ValueError('Release hash mismatch: '+','.join(bad))
 return len(manifest['files'])
