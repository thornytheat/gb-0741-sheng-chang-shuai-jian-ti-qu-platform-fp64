import json
import subprocess
import sys
from copy import deepcopy

import pytest

from app import config
from app.evidence import canonical, sha256
from app.modules.m7_archive import bundle


def exported(client, sample='hall'):
    run = client.post('/api/runs', json={'sample': sample}).json()
    return run, client.get(f"/api/runs/{run['id']}/export").json()


def test_package_is_minimal_recomputable_set_not_raw_intake(client, raw):
    run, package = exported(client)
    assert package['format'] == 'hall-decay-evidence/1' and package['valid']
    assert package['scope']['carries_raw_recording'] is False
    assert package['scope']['included']['samples'] == package['intake']['counts']['accepted']
    # the untouched source recording is not embedded, only adopted amplitudes are
    assert len(canonical(package)) < len(raw)
    assert package['source']['size_bytes'] == len(raw)


def test_every_adopted_point_cites_its_original_line(client):
    run, package = exported(client)
    spans = {band['band_hz']: (band['first_line'], band['last_line']) for band in package['intake']['bands']}
    for entry in package['bands']:
        if not entry['included']:
            continue
        first, last = spans[entry['band_hz']]
        assert len(entry['lines']) == len(entry['amplitudes']) == entry['sample_count']
        # lines follow sample order (arrival order may differ from file order), all within the band span
        assert all(first <= line <= last for line in entry['lines'])
        assert (min(entry['lines']), max(entry['lines'])) == (first, last)


def test_blocked_rows_and_reasons_cite_lines_without_contents(client):
    run, package = exported(client)
    excluded = {entry['band_hz']: entry for entry in package['bands'] if not entry['included']}
    assert sorted(excluded) == [63, 250, 2000]
    band = next(row for row in package['intake']['isolated_bands'] if row['band_hz'] == 63)
    assert excluded[63]['blocked_lines'] == band['blocked_lines'] == [8210]
    assert {finding['code'] for finding in band['findings']} == {'ROW_ISOLATED', 'MISSING_POINTS'}
    # no amplitude text from blocked rows leaks into the package
    assert 'N/A' not in canonical(package).decode()
    assert package['scope']['excluded']['contents_included'] is False
    assert package['scope']['excluded']['rows'] == (package['intake']['counts']['input']
                                                    - package['intake']['counts']['accepted'])
    assert package['scope']['excluded']['blank_lines'] == [8467]
    assert package['scope']['excluded']['repeated_header_lines'] == [8466]
    assert package['scope']['excluded']['unassigned_rows'] == [8468]


def test_gaps_and_truncation_cite_bracketing_lines(client):
    run, package = exported(client)
    findings = {row['band_hz']: row['findings']
                for row in package['intake']['isolated_bands']}
    gap = next(item for item in findings[250] if item['code'] == 'MISSING_POINTS')
    assert gap['indices'] == [100]
    assert gap['gaps'] == [{'index': 100, 'before_line': 1201, 'after_line': 1202}]
    tail = next(item for item in findings[2000] if item['code'] == 'TRUNCATED_BAND')
    assert (tail['from_index'], tail['to_index'], tail['after_line']) == (1062, 1099, 5465)
    conflict = next(item for item in findings[2000] if item['code'] == 'DUPLICATE_CONFLICT')
    assert conflict['lines'] == [4417, 4418] and 'values' not in conflict


def test_every_stage_conclusion_traces_back_to_source_lines(client):
    run, package = exported(client)
    spans = {row['band_hz']: (row['first_line'], row['last_line']) for row in package['intake']['bands']}
    conclusion = {row['band_hz']: row for row in package['conclusion']['bands']}
    assert set(conclusion) == {125, 500, 1000, 4000, 8000} | {63, 250, 2000}
    recomputed = bundle.recompute(package)
    for row in recomputed['calibration']['bands']:
        if row['band_hz'] in spans:
            low, high = spans[row['band_hz']]
            assert low <= row['peak_line'] <= high
    for row in recomputed['fit']['bands']:
        for trial in ('t20', 't30'):
            window = row[trial]['window']
            if window and row['band_hz'] in spans:
                assert spans[row['band_hz']][0] <= window['start_line'] <= window['end_line'] <= spans[row['band_hz']][1]


def test_package_digest_is_self_describing_and_stable(client):
    run, package = exported(client)
    body = {key: value for key, value in package.items() if key != 'digest'}
    assert package['digest'] == sha256(canonical(body))
    assert package['checks'] == {
        'included_samples_match_accepted': True,
        'row_accounting_balanced': True,
        'recomputed_matches_archive': True,
    }


def test_tampered_package_fails_digest_or_recompute(client):
    run, package = exported(client)
    forged = deepcopy(package)
    named = next(entry for entry in forged['bands'] if entry['included'])
    named['amplitudes'][0] = '1'
    with pytest.raises(Exception) as failure:
        bundle.verify(forged)
    assert getattr(failure.value, 'code', None) == 'EVIDENCE_CHANGED'
    # re-sealing the digest cannot hide a changed amplitude: the archived digest no longer matches
    forged['digest'] = sha256(canonical({key: value for key, value in forged.items() if key != 'digest'}))
    report = bundle.check(forged)
    assert report['valid'] and report['recomputed_matches_archive'] is False


def test_independent_recompute_from_json_matches_archive(client):
    run, package = exported(client)
    recomputed = bundle.recompute(package)
    assert sha256(canonical(recomputed)) == package['archive']['result_digest']
    assert recomputed['reconcile']['totals']['flagged'] == 1


def test_cli_recomputes_without_store(tmp_path, client):
    run, package = exported(client, 'reference')
    path = tmp_path / 'package.json'
    path.write_text(json.dumps(package, ensure_ascii=False), encoding='utf-8')
    root = str(config.APP_DIR.parent)
    finished = subprocess.run([sys.executable, '-m', 'app.modules.m7_archive.bundle', str(path)],
                              cwd=tmp_path, capture_output=True, text=True,
                              env={'PYTHONPATH': root, 'PYTHONDONTWRITEBYTECODE': '1', 'PATH': '/usr/bin:/bin'})
    report = json.loads(finished.stdout)
    assert finished.returncode == 0 and report['ok'] and report['recomputed_matches_archive']
    assert report['totals']['fitted'] == 2


def test_old_rule_version_package_is_marked_stale(client, monkeypatch):
    run, package = exported(client)
    assert package['valid'] is True
    monkeypatch.setattr(config, 'RULE_VERSION', 'rt-schroeder-2')
    stale = client.get(f"/api/runs/{run['id']}/export")
    body = stale.json()
    assert body['valid'] is False and body['stale']['code'] == 'RULE_VERSION_CHANGED'
    assert body['stale']['archived_version'] == 'rt-schroeder-1'
    assert body['stale']['current_version'] == 'rt-schroeder-2'
    assert body['scope']['recomputable'] is False
    assert stale.headers['x-evidence-valid'] == 'false'
    assert client.post(f"/api/runs/{run['id']}/rerun").status_code == 409
    view = client.get(f"/api/runs/{run['id']}/package").json()
    assert view['valid'] is False
    # a stale package is not presented as a current-rule recomputation
    assert bundle.check(body)['recomputed_matches_archive'] is False


def test_repackaging_is_idempotent(client):
    run = client.post('/api/runs', json={'sample': 'hall'}).json()
    first = client.get(f"/api/runs/{run['id']}/export").content
    second = client.get(f"/api/runs/{run['id']}/export").content
    again = client.post(f"/api/runs/{run['id']}/rerun").json()
    third = client.get(f"/api/runs/{run['id']}/export").content
    assert first == second == third
    assert again['reused'] and len(client.get('/api/runs').json()['runs']) == 1


def test_workbench_package_preview_lists_inclusion(client):
    run = client.post('/api/runs', json={'sample': 'hall'}).json()
    view = client.get(f"/api/runs/{run['id']}/package").json()
    assert view['scope']['carries_raw_recording'] is False
    assert view['scope']['included']['bands'] == [125, 500, 1000, 4000, 8000]
    assert all('amplitudes' not in entry and 'lines' not in entry for entry in view['bands'])
    assert view['digest'] == client.get(f"/api/runs/{run['id']}/export").json()['digest']


def test_verify_endpoint_recomputes_delivered_package(client):
    run = client.post('/api/runs', json={'sample': 'hall'}).json()
    package = client.get(f"/api/runs/{run['id']}/export").json()
    reply = client.post(f"/api/runs/{run['id']}/verify", json=package)
    assert reply.status_code == 200 and reply.json()['recomputed_matches_archive']
    package['scope']['excluded']['contents_included'] = True
    reply = client.post(f"/api/runs/{run['id']}/verify", json=package)
    assert (reply.status_code, reply.json()['code']) == (409, 'EVIDENCE_CHANGED')
