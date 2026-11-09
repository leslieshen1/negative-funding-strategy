"""Validate aggregate results, freeze provenance and package an explicit allowlist."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import zipfile
import numpy as np
import pandas as pd
import acquire_phase1 as A

REPO = Path(__file__).resolve().parent.parent


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path):
    def invalid(value):
        raise ValueError('Non-finite JSON value: '+value)
    return json.loads(Path(path).read_text(), parse_constant=invalid)


def validate():
    root = A.ROOT
    bridge = read(root/'bridge_final/results.json')['cases']
    native = next(v for v in bridge if v['case'] == 'native_0p1')
    extended = next(v for v in bridge if v['case'] == 'extended_engine_legacy_compatibility')
    assert abs(native['ann_mtm_pct']-75.16414173387383) < 1e-9
    for key in ['net_usdt','funding_net_usdt','fees_usdt','pair_price_including_impact_usdt']:
        assert abs(native[key]-extended[key]) < 1e-6
    runs = 0
    for folder in ['bridge_final','lifecycle_verified','followup_final','concentration', 'candidate_balanced','candidate_lower_oi']:
        assert read(root/folder/'VALIDATION.json')['completed']
        data = read(root/folder/'results.json')['cases']
        for v in data:
            runs += 1
            assert np.isfinite(v['net_usdt'])
            assert abs(v['ann_mtm_pct']-v['net_usdt']/1e7*8760/v['hours']*100) < 1e-9
            assert v['inventory_cap_passed']
            assert (v['ann_completed_pct'] is None) or v['terminal_residual_symbols'] == 0
            assert v['actual_execution_verified'] is False
            if v['ann_completed_pct'] is not None:
                assert abs(v['net_usdt']-(v['funding_net_usdt']+v['pair_price_including_impact_usdt']-v['fees_usdt'])) < .002
    results = read(root/'concentration/results.json')['cases']
    preferred = next(v for v in results if v['case']=='causal_reference' and v['window']=='FY')
    assert preferred['profile_file_sha256'] == sha(REPO/'profiles/candidate_diversified.json')
    assert preferred['hours'] == 8976
    assert abs(preferred['net_usdt']-6709412.878151508) < 1e-6
    for v in results:
        assert v['terminal_residual_symbols'] == 0
        assert v['accounts']['one_x_initial_margin_shortfall_hours'] == 0
        assert v['accounts']['joint_ledger_max_error_usdt'] < .002
        assert v['accounts']['A_negative_cash_hours'] == v['accounts']['B_negative_wallet_hours'] == 0
    profile_matches = [('candidate_balanced','liq2_oi5'),('candidate_lower_oi','liq5_oi2p5')]
    follow = read(root/'followup_final/results.json')['cases']
    for folder, name in profile_matches:
        for v in read(root/folder/'results.json')['cases']:
            match = next(x for x in follow if x['case']==name and x['window']==v['window'])
            assert abs(v['net_usdt']-match['net_usdt']) < 1e-6
    monthly = pd.read_csv(root/'concentration/monthly_pnl.csv')
    assert abs(monthly.pnl_usdt.sum()-preferred['net_usdt']) < 1e-6
    assert read(root/'minute_diversified/minute_sample_summary.json')['position_input_sha256'] == sha(root/'concentration/private/reference_positions.npz')
    log = (root/'TESTS.log').read_text()
    assert 'Ran 17 tests' in log and log.rstrip().endswith('OK')
    inputs = read(root/'INPUT_VALIDATION.json')
    assert inputs['status'] == 'passed' and not inputs['archive_errors'] and not inputs['missing_archive_files']
    manifest = read(root/'data/MANIFEST.json')
    for name, digest in manifest['hashes'].items():
        assert sha(root/'data'/name) == digest
    A.js(root/'FINAL_VALIDATION.json', dict(status='passed', aggregate_case_window_runs_checked=runs,
        native_baseline_and_extended_default_exact_match=True, profile_cli_cases_match_labelled_experiments=True,
        all_preferred_and_stress_windows_zero_terminal_obligations=True, preferred_account_identity_passed=True,
        preferred_one_x_margin_checks_passed=True, monthly_to_total_identity_passed=True,
        minute_audit_position_hash_verified=True, real_JIT_boundary_tests=17, input_hashes_verified=4,
        actual_execution_verified=False, historical_quantity_filters_verified=False,
        research_delivery_complete=True, production_validation_complete=False))
    base = '6cd6c23d6af5e0b92a3840fe44e09655d8adaed0'
    files = subprocess.check_output(['git','ls-tree','-r','--name-only',base], cwd=REPO, text=True).splitlines()
    hashes = {name:hashlib.sha256(subprocess.check_output(['git','show',f'{base}:{name}'],cwd=REPO)).hexdigest() for name in files}
    A.js(root/'SOURCE_PROVENANCE.json', dict(original_v1_commit=base, original_file_sha256=hashes,
        original_file_count=len(files), existing_v1_workspace_modified_by_this_research=False,
        extended_engine_origin='Local v2 engine with explicit funding-event and account audit fixes; additional risk parameters are opt-in.',
        calibration_stream_origin='Prior independent rebuild_calibration.py, streaming reducer and downloader retained; old rebuild-main omitted.',
        expanded_input_dependencies='Old 315-cohort data and calibration are required local dependencies; not included in portable source archive.',
        research_profile_sha256={p.name:sha(p) for p in sorted((REPO/'profiles').glob('*.json'))}))
    allowed = ['family','symbol','sym','date','tag','url','status','sha256','bytes','zip_sha256','zip_bytes',
               'official_checksum_verified','fetched_at_utc','reason','http_status']
    with (root/'PUBLIC_DATA_PROVENANCE.jsonl').open('w') as f:
        for folder in [root/'raw',root/'records',A.PRIOR/'records']:
            for path in sorted(folder.rglob('*.json')):
                rec = read(path)
                if 'url' not in rec:
                    continue
                public = {k:rec[k] for k in allowed if k in rec}
                public['origin_group'] = str(folder.relative_to(root)) if folder.is_relative_to(root) else 'prior_calibration_records'
                f.write(json.dumps(public, allow_nan=False)+'\n')
    docs = REPO/'docs'
    copies = {'第一部分研究报告.md':'PHASE1_RESEARCH_REPORT.md','第一部分验收清单.md':'PHASE1_ACCEPTANCE.md',
              'RESULT_SUMMARY.json':'RESULT_SUMMARY.json','CANDIDATE_RESOLVED_PARAMETERS.json':'CANDIDATE_RESOLVED_PARAMETERS.json',
              'SOURCE_PROVENANCE.json':'SOURCE_PROVENANCE.json','FINAL_VALIDATION.json':'FINAL_VALIDATION.json'}
    for source, dest in copies.items():
        shutil.copy2(root/source, docs/dest)
    (docs/'figures').mkdir(exist_ok=True)
    for p in (root/'figures').glob('*.png'):
        shutil.copy2(p, docs/'figures'/p.name)
    for path in root.glob('*/private'):
        path.chmod(0o700)
    (root/'private').chmod(0o700)
    print('PASS: final evidence validation;', runs, 'case-window runs')


def package():
    root = A.ROOT
    destination = root/'phase1-evidence.zip'
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    status = subprocess.check_output(['git','status','--porcelain'],cwd=REPO,text=True)
    assert not status, 'Commit the reviewed source before packaging'
    files = {}
    source_names = subprocess.check_output(['git','ls-files'],cwd=REPO,text=True).splitlines()
    for name in source_names:
        assert name in ['.gitignore','README.md','PUBLIC_EXCLUDE.md'] or name.startswith(('research/','profiles/','docs/','repro/'))
        files['source/'+name] = REPO/name
    for name in ['第一部分研究报告.md','第一部分验收清单.md','RESULT_SUMMARY.json','CANDIDATE_RESOLVED_PARAMETERS.json',
                 'FINAL_VALIDATION.json','SOURCE_PROVENANCE.json','INPUT_VALIDATION.json','PORTFOLIO_AUDIT.json',
                 'PUBLIC_DATA_PROVENANCE.jsonl','TESTS.log','instrument_mapping_audit.csv','current_quantity_steps.csv',
                 'acquisition_summary.json','funding_api_summary.json','extra_calibration_summary.json','universe.json']:
        files[name] = root/name
    files['data_input_manifest.json'] = root/'data/MANIFEST.json'
    for p in (root/'figures').glob('*.png'):
        files['figures/'+p.name] = p
    for folder in ['bridge_final','lifecycle_verified','followup_final','concentration','candidate_balanced','candidate_lower_oi']:
        for name in ['results.json','VALIDATION.json','weekly_uncertainty.json','concentration_audit.json','monthly_pnl.csv','T1_POLICY.json']:
            p = root/folder/name
            if p.exists():
                files[f'results/{folder}/{name}'] = p
        for p in (root/folder).glob('*_FY_curve.csv'):
            files[f'results/{folder}/{p.name}'] = p
    for name in ['minute_sample_summary.json','minute_sample_audit.csv']:
        files['results/minute_diversified/'+name] = root/'minute_diversified'/name
    files['results/concentration/account_audit_FY.csv'] = root/'concentration/account_audit_FY.csv'
    for name in files:
        assert '/private/' not in name and '.npz' not in name and not name.endswith('.key')
    manifest = dict(source_commit=commit, branch='codex/strategy-phase1-20261106',
                    file_sha256={name:sha(path) for name,path in sorted(files.items())},
                    private_inventory_and_position_files_included=False,
                    full_market_matrix_and_raw_archives_included=False,
                    omitted_dependencies='Private inventory, old 315-cohort data/calibration, expanded matrix and raw archives retained locally. See source/README.md.',
                    actual_execution_verified=False)
    A.js(root/'EVIDENCE_MANIFEST.json',manifest)
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,path in sorted(files.items()):
            z.write(path,name)
        z.write(root/'EVIDENCE_MANIFEST.json','EVIDENCE_MANIFEST.json')
    with zipfile.ZipFile(destination) as z:
        assert z.testzip() is None
        for name,digest in manifest['file_sha256'].items():
            assert hashlib.sha256(z.read(name)).hexdigest()==digest
    A.js(root/'ARTIFACT_SHA256.json',dict(file=destination.name,sha256=sha(destination),bytes=destination.stat().st_size,
        members=len(files)+1,archive_crc_and_member_sha256_passed=True,source_commit=commit))
    print('PASS: explicit-allowlist ZIP, CRC and member SHA256;',destination.stat().st_size,'bytes')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['validate','package'])
    args = parser.parse_args()
    validate() if args.action=='validate' else package()
