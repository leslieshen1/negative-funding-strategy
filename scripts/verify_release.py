"""Verify the frozen runtime and observable historical example, without network access."""
import subprocess,sys,json
from package_tools import ROOT,verify_manifest
n=verify_manifest()
for path in ['runtime/verify_package.py','support/verify_support.py']:
 subprocess.run([sys.executable,'-B',str(ROOT/path)],check=True,cwd=ROOT)
result=subprocess.run([sys.executable,'-B',str(ROOT/'runtime/decision.py'),'--input',str(ROOT/'runtime/runtime_examples/01_historical_market_enter.input.json'),'--replay'],capture_output=True,text=True,check=True,cwd=ROOT)
got=json.loads(result.stdout);expected=json.loads((ROOT/'runtime/runtime_examples/01_historical_market_enter.output.json').read_text())
if got!=expected:raise ValueError('Frozen historical decision differs from original expected output')
if got['submission_allowed'] is not False:raise ValueError('Unexpected execution permission')
print(json.dumps({'passed':True,'release_files':n,'frozen_historical_output_matches':True,'submission_allowed':False}))
