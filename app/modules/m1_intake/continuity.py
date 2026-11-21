from collections import defaultdict

from app.reasons import reason
from app.records import Band


def issue(line, hz, code, raw):
    return {'line': line, 'band_hz': hz, 'raw': raw, **reason(code)}


def spans(lines):
    """Compress sorted source lines into inclusive ranges."""
    ranges = []
    for line in sorted(lines):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1][1] = line
        else:
            ranges.append([line, line])
    return ranges


def between(index, indices, by_index):
    """Source lines of the nearest present samples on either side of a gap.

    A gap has no row of its own; the citation is the pair of physical lines
    that bracket it, so a reviewer can confirm the omission in the original.
    """
    before = [value for value in indices if value < index]
    after = [value for value in indices if value > index]
    return {'index': index,
            'before_line': by_index[before[-1]][0].line if before else None,
            'after_line': by_index[after[0]][0].line if after else None}


def check_band(hz, rows, invalid_rows):
    if not rows:
        return None, {'band_hz': hz, 'sample_count': 0, 'blocked_rows': len(invalid_rows),
                      'blocked_lines': sorted(invalid_rows), 'line_spans': [],
                      'findings': [{**reason('ROW_ISOLATED'), 'lines': sorted(invalid_rows)}]}, [], 0
    first, first_sample = rows[0]
    configs = {}
    by_index = defaultdict(list)
    for calibration, sample in rows:
        configs.setdefault(calibration, sample.line)
        by_index[sample.index].append(sample)
    findings = []
    if invalid_rows:
        findings.append({**reason('ROW_ISOLATED'), 'lines': invalid_rows})
    if len(configs) > 1:
        findings.append({**reason('CAL_CONFLICT'), 'lines': sorted(configs.values())})
    duplicates, accepted = [], []
    for index in sorted(by_index):
        samples = by_index[index]
        origin = samples[0]
        if any(sample.amplitude != origin.amplitude for sample in samples[1:]):
            findings.append({**reason('DUPLICATE_CONFLICT'), 'index': index, 'lines': [sample.line for sample in samples],
                             'values': [str(sample.amplitude) for sample in samples[:12]]})
        else:
            duplicates.extend(issue(sample.line, hz, 'DUPLICATE_ROW', sample.raw) for sample in samples[1:])
        accepted.append(origin)
    indices = sorted(by_index)
    extra = [index for index in indices if index >= first.expected]
    missing = [index for index in range(min(indices[-1] + 1, first.expected)) if index not in by_index]
    if extra:
        findings.append({**reason('EXTRA_POINTS'), 'indices': extra[:20], 'count': len(extra),
                         'lines': sorted(sample.line for index in extra for sample in by_index[index])})
    if missing:
        findings.append({**reason('MISSING_POINTS'), 'indices': missing[:20], 'count': len(missing),
                         'gaps': [between(index, indices, by_index) for index in missing[:20]]})
    if indices[-1] < first.expected - 1:
        findings.append({**reason('TRUNCATED_BAND'), 'from_index': indices[-1] + 1, 'to_index': first.expected - 1,
                         'after_line': max(sample.line for _, sample in rows)})
    if findings:
        return None, {'band_hz': hz, 'sample_count': len(rows), 'blocked_rows': len(invalid_rows),
                      'blocked_lines': sorted(invalid_rows), 'line_spans': spans(sample.line for _, sample in rows),
                      'first_line': first_sample.line, 'last_line': max(sample.line for _, sample in rows),
                      'findings': findings}, [], len(rows)
    return Band(hz, first, accepted), None, duplicates, 0
