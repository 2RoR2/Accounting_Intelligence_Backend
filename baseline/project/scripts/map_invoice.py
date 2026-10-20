"""Conservative labelled-invoice mapper; standard library only. No AI calls."""
import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

LABELS = {
    'supplier': 'supplier_name', 'invoice number': 'invoice_number',
    'date': 'invoice_date', 'currency': 'currency', 'subtotal': 'subtotal',
    'tax amount': 'tax_amount', 'total amount': 'total_amount',
}
HEADERS = {'description': 'description', 'quantity': 'quantity',
           'unit price': 'unit_price', 'line total': 'line_total'}
AMOUNTS = {'subtotal', 'tax_amount', 'total_amount'}


def location(document, page, box, ref, text):
    """Preserve original coordinates and add an explicit top-left rectangle."""
    size = document.get('pages', {}).get(str(page), {}).get('size')
    out = {'page': page, 'text': text, 'source_ref': ref,
           'bbox_original': deepcopy(box), 'page_size': deepcopy(size),
           'bbox': None, 'coordinate_units': 'document_points'}
    if not box or not size:
        return out
    try:
        left, right = float(box['l']), float(box['r'])
        height, width = float(size['height']), float(size['width'])
        if box['coord_origin'] == 'BOTTOMLEFT':
            top, bottom = height - float(box['t']), height - float(box['b'])
        elif box['coord_origin'] == 'TOPLEFT':
            top, bottom = float(box['t']), float(box['b'])
        else:
            return out
        if 0 <= left <= right <= width and 0 <= top <= bottom <= height:
            out['bbox'] = {'l': left, 't': top, 'r': right, 'b': bottom,
                           'coord_origin': 'TOPLEFT'}
    except (KeyError, TypeError, ValueError):
        pass
    return out


def mapped_field(raw=None, value=None, evidence=None, state='missing', candidates=None):
    return {'raw_text': raw, 'value': value, 'evidence': evidence or [],
            'mapping_status': state, 'review_required': True,
            'candidates': candidates or []}


def map_document(document):
    if document.get('schema_name') != 'DoclingDocument':
        raise ValueError('Expected a DoclingDocument JSON export')
    issues, found = [], defaultdict(list)

    def issue(code, detail):
        issues.append({'code': code, 'detail': detail})

    for index, item in enumerate(document.get('texts', [])):
        if item.get('content_layer', 'body') != 'body':
            continue
        text = item.get('text', '')
        match = re.fullmatch(r'\s*([^:\n]+):\s*([^\n]*)\s*', text)
        if not match:
            continue
        key = LABELS.get(' '.join(match[1].casefold().split()))
        if key:
            evidence = [location(document, p.get('page_no'), p.get('bbox'),
                        item.get('self_ref', f'#/texts/{index}'), text)
                        for p in item.get('prov', [])]
            found[key].append({'raw_text': text, 'value': match[2].strip(),
                               'evidence': evidence})

    fields = {}
    for key in LABELS.values():
        candidates = found[key]
        values = {c['value'] for c in candidates}
        if not candidates or values == {''}:
            fields[key] = mapped_field(candidates=candidates)
            issue('UNMAPPED_FIELD', key + ': no non-empty supported label/value found')
        elif len(values) != 1:
            fields[key] = mapped_field(state='ambiguous', candidates=candidates)
            issue('CONFLICTING_VALUES', key + ': multiple distinct values retained')
        else:
            candidate = candidates[0]
            evidence = [e for c in candidates for e in c['evidence']]
            fields[key] = mapped_field(candidate['raw_text'], candidate['value'],
                                       evidence, 'mapped', candidates)
            if not evidence or any(e['bbox'] is None for e in evidence):
                issue('INCOMPLETE_EVIDENCE', key)

    # Only remove an explicit currency token when it agrees with the currency field.
    # Locale/thousands/date normalisation belongs to the agreed downstream policy.
    for key in AMOUNTS:
        f = fields[key]
        if f['value'] is None:
            continue
        match = re.fullmatch(r'(?:(?P<currency>[A-Z]{3})\s+)?(?P<amount>-?\d+(?:\.\d+)?)', f['value'])
        if not match or (match['currency'] and match['currency'] != fields['currency']['value']):
            f['value'], f['mapping_status'] = None, 'ambiguous'
            issue('AMOUNT_REQUIRES_REVIEW', key + ': unsupported format or currency disagreement')
        else:
            f['value'] = match['amount']

    rows = []
    for ti, table in enumerate(document.get('tables', [])):
        ref = table.get('self_ref', f'#/tables/{ti}')
        data, prov = table.get('data', {}), table.get('prov', [])
        cells = data.get('table_cells', [])
        if len(prov) != 1 or not prov[0].get('page_no'):
            issue('UNSUPPORTED_TABLE', ref + ': page identity is not unambiguous')
            continue
        if data.get('orientation', 'rot_0') != 'rot_0' or any(
            c.get('row_span', 1) != 1 or c.get('col_span', 1) != 1 for c in cells
        ):
            issue('UNSUPPORTED_TABLE', ref + ': rotated or merged cells require review')
            continue
        columns, bad_header = {}, False
        for c in cells:
            if c.get('column_header'):
                name = HEADERS.get(' '.join(c.get('text', '').casefold().split()))
                col = c.get('start_col_offset_idx')
                if c.get('start_row_offset_idx') != 0 or not name or col in columns or name in columns.values():
                    bad_header = True
                columns[col] = name
        if bad_header or set(columns.values()) != set(HEADERS.values()) or data.get('num_cols') != 4:
            issue('UNSUPPORTED_TABLE', ref + ': expected four unique supported column headers')
            continue
        row_count = data.get('num_rows', 0)
        if not isinstance(row_count, int) or row_count < 2:
            issue('UNSUPPORTED_TABLE', ref + ': no data rows')
            continue
        grouped = defaultdict(list)
        for ci, cell in enumerate(cells):
            if not cell.get('column_header'):
                grouped[cell.get('start_row_offset_idx')].append((ci, cell))
        if any(r not in range(1, row_count) for r in grouped):
            issue('UNSUPPORTED_TABLE', ref + ': unexpected data row indices')
            continue
        for ri in range(1, row_count):
            row = {}
            for col, key in columns.items():
                matches = [(ci, c) for ci, c in grouped[ri] if c.get('start_col_offset_idx') == col]
                if len(matches) != 1:
                    row[key] = mapped_field(state='ambiguous' if matches else 'missing')
                    issue('INCOMPLETE_TABLE_ROW', f'{ref} row {ri} field {key}')
                    continue
                ci, cell = matches[0]
                text = cell.get('text', '')
                ev = location(document, prov[0]['page_no'], cell.get('bbox'),
                              f'{ref}/data/table_cells/{ci}', text)
                value = text.strip() or None
                row[key] = mapped_field(text, value, [ev], 'mapped' if value else 'missing')
                if not value or ev['bbox'] is None:
                    issue('INCOMPLETE_TABLE_EVIDENCE', f'{ref} row {ri} field {key}')
            rows.append(row)
    if not rows:
        issue('NO_MAPPED_LINE_ITEMS', 'No supported line-item rows mapped; inspect original tables')
    issue('HUMAN_REVIEW_REQUIRED', 'Proposed extraction only; no accounting validation or approval performed')
    return {'schema_version': 'draft-0.2', 'mode': 'real_parser_mapping',
            'document_id': document.get('origin', {}).get('filename') or document.get('name'),
            'status': 'needs_review', 'fields': fields, 'line_items': rows,
            'issues': issues, 'provenance': {'path': 'docling_explicit_mapping',
            'mapper_version': '0.1.0', 'model': None,
            'docling_schema_version': document.get('version')}}


def compare(result, expected):
    checks = []
    for key, val in expected['fields'].items():
        actual = result['fields'].get(key, {}).get('value')
        checks.append({'field': key, 'expected': val, 'actual': actual, 'match': actual == val})
    checks.append({'field': 'line_item_count', 'expected': len(expected['line_items']),
                   'actual': len(result['line_items']),
                   'match': len(expected['line_items']) == len(result['line_items'])})
    for i, row in enumerate(expected['line_items']):
        actual_row = result['line_items'][i] if i < len(result['line_items']) else {}
        for key, val in row.items():
            actual = actual_row.get(key, {}).get('value')
            checks.append({'field': f'line_items[{i}].{key}', 'expected': val,
                           'actual': actual, 'match': actual == val})
    return {'scope': 'Exact string comparison on this supplied sample only; not general accuracy or evidence verification',
            'passed': sum(c['match'] for c in checks), 'total': len(checks), 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', nargs='?', help='Path to a specific document.json')
    parser.add_argument('--latest', action='store_true', help='Select newest docling-*/document.json by modification time')
    parser.add_argument('--expected', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if bool(args.document) == args.latest:
        parser.error('Provide either document.json path OR --latest')
    if args.latest:
        paths = list((root / 'runs').glob('docling-*/document.json'))
        if not paths:
            parser.error('No parser document.json found under runs/docling-*')
        source = max(paths, key=lambda p: p.stat().st_mtime)
    else:
        source = Path(args.document).resolve()
    print('Mapping source:', source)
    raw = source.read_bytes()
    document = json.loads(raw)
    expected = json.loads(args.expected.read_text(encoding='utf-8')) if args.expected else None
    start = time.perf_counter()
    result = map_document(document)
    result['provenance']['mapping_seconds'] = round(time.perf_counter() - start, 6)
    result['provenance']['source_json_sha256'] = hashlib.sha256(raw).hexdigest()
    result['provenance']['source_document_json'] = str(source)
    run_file = source.with_name('run.json')
    if run_file.exists():
        result['provenance']['parser_run'] = json.loads(run_file.read_text(encoding='utf-8'))
    result['run_id'] = 'mapped-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    out = root / 'runs' / result['run_id']
    out.mkdir(parents=True, exist_ok=False)
    (out / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if expected is not None:
        report = compare(result, expected)
        (out / 'comparison.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(f"Sample comparisons: {report['passed']}/{report['total']} passed")
    print('Saved:', out)
    print('Review is required. This mapper supports explicit labels and simple four-column tables only.')


if __name__ == '__main__':
    main()
