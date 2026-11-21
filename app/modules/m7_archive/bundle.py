"""Minimal recomputable evidence package for third-party review.

The package carries only what is needed to recompute the archived conclusion:
the calibration tuple and adopted amplitudes of each reviewed band, each with a
source line citation, plus a line-cited description of every row that was not
adopted.  Excluded row contents are never copied out of the original recording.

Packages built under an older rule version stay readable but are marked stale
and are not presented as recomputable; they must be re-run under current rules.
"""
from decimal import Decimal

from app import config
from app.evidence import canonical, sha256
from app.errors import Problem
from app.modules.m1_intake.reader import read
from app.modules.m1_intake.service import citation, summary
from app.modules.m3_decay.integral import TAIL_FRACTION
from app.modules.m4_fit.windows import MIN_POINTS, MIN_R2
from app.modules.m5_noise.gate import MARGIN_DB
from app.modules.m6_reconcile.comparison import DISAGREEMENT_LIMIT
from app.modules.m6_reconcile.service import evaluate
from app.modules.m7_archive.service import record
from app.records import Band, Calibration, Intake, Sample

FORMAT = 'hall-decay-evidence/1'

CONTRACT = {
    'method': '经校准压力平方的反向积分，尾段噪声扣除后取单调剩余能量',
    'pressure': 'amplitude * SCALE[unit] * pa_per_v / 10^(gain_db/20)',
    'origin': '校准后首个绝对振幅最大点',
    'tail_fraction': TAIL_FRACTION,
    'tail_blocks': 5,
    'noise_margin_db': MARGIN_DB,
    't20_db': [-5, -25],
    't30_db': [-5, -35],
    'min_points': MIN_POINTS,
    'min_r2': MIN_R2,
    'disagreement_limit': DISAGREEMENT_LIMIT,
    'rt60': '-60 / slope，秒；勾稽比较未截断数值',
}


def _stale(manifest):
    archived, current = manifest['version'], config.RULE_VERSION
    if archived == current:
        return None
    return {'code': 'RULE_VERSION_CHANGED', 'message': '规则版本已更新，该复核包失效，请按当前规则重跑',
            'archived_version': archived, 'current_version': current}


def _included_entry(band, status):
    return {
        'band_hz': band.hz, 'status': status, 'included': True,
        'sample_count': len(band.samples),
        'first_line': band.samples[0].line, 'last_line': band.samples[-1].line,
        'calibration': band.calibration.public(),
        'lines': [sample.line for sample in band.samples],
        'amplitudes': [str(sample.amplitude) for sample in band.samples],
    }


def _excluded_entry(row, status):
    return {
        'band_hz': row['band_hz'], 'status': status or 'ISOLATED', 'included': False,
        'sample_count': row.get('sample_count', 0), 'blocked_rows': row.get('blocked_rows', 0),
        'blocked_lines': row.get('blocked_lines', []), 'line_spans': row.get('line_spans', []),
        'first_line': row.get('first_line'), 'last_line': row.get('last_line'),
        'findings': [citation(finding) for finding in row.get('findings', [])],
    }


def _band_entries(data, result):
    status = {row['band_hz']: row['status'] for row in result['reconcile']['bands']}
    accepted = {band.hz: band for band in data.bands}
    blocked = {row['band_hz']: row for row in data.rejected_bands}
    entries = []
    for hz in sorted(accepted.keys() | blocked.keys()):
        if hz in accepted:
            entries.append(_included_entry(accepted[hz], status.get(hz)))
        else:
            entries.append(_excluded_entry(blocked[hz], status.get(hz)))
    return entries


def _archived_entries(result):
    """Entries derivable from the archived result when the source cannot be trusted."""
    status = {row['band_hz']: row['status'] for row in result['reconcile']['bands']}
    calibration = {row['band_hz']: row['input'] for row in result['calibration']['bands']}
    intake = result['intake']
    entries = []
    for band in intake['bands']:
        entries.append({'band_hz': band['band_hz'], 'status': status.get(band['band_hz']), 'included': True,
                        'sample_count': None, 'first_line': band['first_line'], 'last_line': band['last_line'],
                        'calibration': calibration.get(band['band_hz']),
                        'amplitudes_included': False,
                        'note': '规则版本已更新，采信振幅未随包保留'})
    for band in intake['isolated_bands']:
        entries.append(_excluded_entry(band, status.get(band['band_hz'])))
    entries.sort(key=lambda entry: entry['band_hz'])
    return entries


def _scope(entries, data, recomputable):
    included = [entry for entry in entries if entry['included']]
    excluded = [entry for entry in entries if not entry['included']]
    counts = data.counts
    lines = data.excluded_lines or {}
    return {
        'carries_raw_recording': False,
        'recomputable': recomputable,
        'included': {'bands': [entry['band_hz'] for entry in included],
                     'samples': counts['accepted'] if recomputable
                     else sum(entry['sample_count'] or 0 for entry in included),
                     'fields': ['calibration', 'lines', 'amplitudes'] if recomputable
                     else ['calibration', 'first_line', 'last_line'],
                     'note': '仅采信采样点的校准参数、来源行与振幅；足以重算曲线、拟合与勾稽' if recomputable
                     else '包已失效，仅保留归档结论与被挡记录行号，不重算'},
        'excluded': {'bands': [{'band_hz': entry['band_hz'], 'blocked_rows': entry['blocked_rows'],
                                'blocked_lines': entry['blocked_lines'],
                                'reason_codes': [finding['code'] for finding in entry['findings']]}
                               for entry in excluded],
                     'rows': counts['input'] - counts['accepted'],
                     'blank_lines': list(lines.get('blank', [])),
                     'repeated_header_lines': list(lines.get('repeated_headers', [])),
                     'unassigned_rows': [row['line'] for row in data.row_issues if row['band_hz'] is None],
                     'contents_included': False,
                     'note': '被挡记录只保留来源行与排除原因，不携带振幅原文'},
    }


def _conclusion(result):
    """Compact archived conclusion for display and spot-checking.

    The full report is not shipped; reviewers recompute it from the adopted
    amplitudes and compare the digest to ``archive.result_digest``.
    """
    return {
        'reconcile': {'totals': result['reconcile']['totals'], 'status': result['reconcile']['status'],
                      'unassigned_rows': result['reconcile']['unassigned_rows']},
        'bands': [{'band_hz': row['band_hz'], 'status': row['status'], 't20_s': row['t20_s'],
                   't30_s': row['t30_s'], 'relative_gap': row['relative_gap'],
                   'gap_flag': row['gap_flag'], 'findings': [item['code'] for item in row['findings']]}
                  for row in result['reconcile']['bands']],
        'noise': [{'band_hz': row['band_hz'], 'usable_depth_db': row['usable_depth_db'],
                   'noise_power_pa2': row['noise_power_pa2'],
                   'error': row['error']['code'] if row['error'] else None}
                  for row in result['noise']['bands']],
    }


def _checks(package, entries):
    counts = package['intake']['counts']
    included = [entry for entry in entries if entry['included']]
    return {
        'included_samples_match_accepted': (sum(entry['sample_count'] for entry in included) == counts['accepted'])
        if package['valid'] else None,
        'row_accounting_balanced': sum(value for key, value in counts.items() if key != 'input') == counts['input'],
    }


def rebuild(package):
    """Reconstruct the adopted intake from the package alone."""
    if not package['valid']:
        raise Problem('VERSION_CHANGED', '该复核包已失效，不能按当前规则重算', 409)
    bands = []
    for entry in package['bands']:
        if not entry['included']:
            continue
        cal = entry['calibration']
        calibration = Calibration(cal['sample_rate_hz'], cal['expected_samples'], cal['unit'],
                                  Decimal(cal['gain_db']),
                                  Decimal(cal['pa_per_v']) if cal['pa_per_v'] is not None else None)
        samples = [Sample(line, index, Decimal(amplitude), {})
                   for index, (line, amplitude) in enumerate(zip(entry['lines'], entry['amplitudes']))]
        bands.append(Band(entry['band_hz'], calibration, samples))
    if not bands:
        raise Problem('EVIDENCE_CHANGED', '证据包没有可重算的采信频带', 409)
    intake = package['intake']
    return Intake(bands, intake['isolated_bands'], intake['row_issues'], intake['counts'],
                  package['source']['encoding'], intake.get('excluded_lines'))


def recompute(package):
    """Rebuild the full archived result from the package alone."""
    data = rebuild(package)
    return {'intake': summary(package['source']['source_id'], data), **evaluate(data)}


def _load_source(store, source_id):
    with store.connect() as connection:
        row = connection.execute('SELECT content FROM source WHERE id=?', (source_id,)).fetchone()
    if row is None:
        raise Problem('SOURCE_MISSING', '收件记录不存在', 404)
    return row['content']


def build(store, run_id):
    archive = record(store, run_id)
    manifest, result = archive['manifest'], archive['result']
    intake = result['intake']
    stale = _stale(manifest)
    content = None
    if stale is None:
        content = _load_source(store, manifest['source_id'])
        data = read(content)
        if summary(manifest['source_id'], data) != intake:
            raise Problem('EVIDENCE_CHANGED', '来源与归档收件证据不符', 409)
        entries = _band_entries(data, result)
        scope = _scope(entries, data, True)
    else:
        entries = _archived_entries(result)
        scope = _scope(entries, _ArchivedIntake(intake), False)
    package = {
        'format': FORMAT,
        'rule_version': manifest['version'],
        'valid': stale is None,
        'stale': stale,
        'archive': {'run_id': manifest['id'], 'created_at': manifest['created_at'],
                    'elapsed_ms': manifest['elapsed_ms'], 'result_digest': manifest['digest']},
        'source': {'source_id': manifest['source_id'],
                   'size_bytes': len(content) if content is not None else None,
                   'encoding': intake['encoding']},
        'contract': CONTRACT,
        'scope': scope,
        'intake': intake,
        'bands': entries,
        'conclusion': _conclusion(result),
    }
    package['checks'] = _checks(package, entries)
    if stale is None:
        recomputed = recompute(package)
        package['checks']['recomputed_matches_archive'] = sha256(canonical(recomputed)) == manifest['digest']
        if not package['checks']['recomputed_matches_archive']:
            raise Problem('EVIDENCE_CHANGED', '证据包重算结果与归档不一致', 409)
    package['digest'] = sha256(canonical(package))
    return package


class _ArchivedIntake:
    """Read-only view of the archived intake summary for stale packages."""

    def __init__(self, intake):
        self.counts = intake['counts']
        self.excluded_lines = intake.get('excluded_lines', {})
        self.row_issues = intake['row_issues']


def preview(package):
    """Package description for the workbench, without adopted amplitudes or line lists."""
    view = {key: value for key, value in package.items() if key not in {'bands', 'intake'}}
    view['bands'] = [{key: value for key, value in entry.items() if key not in {'amplitudes', 'lines'}}
                     for entry in package['bands']]
    view['intake_counts'] = package['intake']['counts']
    return view


def verify(package):
    """Check a received package's digest."""
    body = {key: value for key, value in package.items() if key != 'digest'}
    if package.get('digest') != sha256(canonical(body)):
        raise Problem('EVIDENCE_CHANGED', '证据包摘要不符', 409)
    return package['digest']


def check(package):
    """Verify the digest and whether an independent recompute reproduces the archive."""
    digest = verify(package)
    if not package.get('valid'):
        return {'digest': digest, 'valid': False, 'stale': package.get('stale'),
                'recomputed_matches_archive': False, 'conclusion': None}
    recomputed = recompute(package)
    matches = sha256(canonical(recomputed)) == package['archive']['result_digest']
    return {'digest': digest, 'valid': True, 'stale': None,
            'recomputed_matches_archive': matches,
            'conclusion': _conclusion(recomputed),
            'totals': recomputed['reconcile']['totals']}


def main(argv=None):
    """Recompute a delivered package from the JSON alone, without the store."""
    import json
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print('用法: python -m app.modules.m7_archive.bundle <证据包.json>', file=sys.stderr)
        return 2
    with open(args[0], encoding='utf-8') as handle:
        package = json.load(handle)
    try:
        report = check(package)
    except Problem as error:
        print(json.dumps({'ok': False, 'code': error.code, 'message': error.message}, ensure_ascii=False))
        return 1
    report['ok'] = bool(report['valid'] and report['recomputed_matches_archive'])
    report['format'] = package.get('format')
    report['rule_version'] = package.get('rule_version')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
