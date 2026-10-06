"""Conservative heading-rule baseline over Docling text; synthetic evaluation only."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

VERSION = '0.1.0'
TITLES = {
    'invoice': 'invoice', 'tax invoice': 'invoice', 'commercial invoice': 'invoice',
    'bill': 'bill', 'utility bill': 'bill',
    'receipt': 'receipt', 'sales receipt': 'receipt', 'payment receipt': 'receipt',
}
UNSUPPORTED = {'credit note', 'debit note', 'quotation', 'purchase order',
               'pro forma invoice', 'proforma invoice', 'statement of account'}

def normalize_title(text):
    text = ' '.join(text.casefold().split())
    # The existing synthetic invoice uses this exact decorative suffix.
    text = re.sub(r'\s*[-–—]\s*synthetic test data$', '', text)
    return text.strip()

def classify_document(document):
    if document.get('schema_name') != 'DoclingDocument':
        raise ValueError('Expected a DoclingDocument export')
    candidates, excluded = [], []
    for index, item in enumerate(document.get('texts', [])):
        if item.get('content_layer', 'body') != 'body':
            continue
        # Text allows Docling's unlabelled-heading cases, but requires a whole
        # text item to match. We never match an invoice number or 'Bill to'.
        if item.get('label', 'text') not in {'title', 'section_header', 'text'}:
            continue
        raw = item.get('text', '')
        title = normalize_title(raw)
        if title not in TITLES and title not in UNSUPPORTED:
            continue
        evidence = {'text': raw,
                    'source_ref': item.get('self_ref', f'#/texts/{index}'),
                    'prov': deepcopy(item.get('prov', []))}
        if title in UNSUPPORTED:
            excluded.append({'matched_title': title, 'evidence': evidence})
        else:
            candidates.append({'value': TITLES[title], 'matched_title': title,
                               'evidence': evidence})
    values = sorted({c['value'] for c in candidates})
    if excluded:
        value, status, reason = 'unknown', 'unsupported', 'UNSUPPORTED_DOCUMENT_HEADING'
    elif len(values) > 1:
        value, status, reason = 'unknown', 'ambiguous', 'MULTIPLE_DOCUMENT_TYPES'
    elif len(values) == 1:
        value, status, reason = values[0], 'classified', 'EXPLICIT_DOCUMENT_HEADING'
    else:
        value, status, reason = 'unknown', 'unknown', 'NO_SUPPORTED_DOCUMENT_HEADING'
    return {'classifier_version': VERSION, 'method': 'explicit_heading_rules',
            'value': value, 'status': status, 'reason': reason,
            'candidates': candidates, 'unsupported_headings': excluded,
            'review_required': True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document_json', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = args.document_json.resolve()
    if not source.is_relative_to(root):
        parser.error('Input must be inside the SSD project')
    try:
        raw = source.read_bytes()
        result = classify_document(json.loads(raw))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.error(str(error))
    result['source_document_json'] = str(source)
    result['source_json_sha256'] = hashlib.sha256(raw).hexdigest()
    out = root / 'runs' / ('classification-' + uuid4().hex)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('Classification:', result['value'])
    print('Status:', result['status'])
    print('Reason:', result['reason'])
    print('Matched text:', [c['evidence']['text'] for c in result['candidates']])
    print('Saved:', out)
    print('Rule baseline only; no AI call or accounting approval.')

if __name__ == '__main__':
    main()


