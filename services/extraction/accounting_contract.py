"""Versioned M2 -> existing accounting storage/API boundary. No approval or DB I/O."""
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, localcontext
import re
from uuid import UUID

VERSION = 'accounting-fields/1.1'
FIELD_KEYS = {
    'supplier_name': 'supplier', 'vendor_name': 'supplier', 'seller': 'supplier', 'merchant': 'supplier',
    'invoice_number': 'invoice', 'invoice_date': 'date', 'due_date': 'due',
    'tax_amount': 'tax', 'total_amount': 'total', 'buyer_name': 'customer', 'buyer': 'customer',
    'reference_number': 'reference', 'receipt': 'reference',
    'issuer': 'supplier', 'bill_reference': 'reference', 'bill_date': 'date',
    'receipt_number': 'reference', 'transaction_date': 'date', 'amount_due': 'total',
    'account_number': 'account_number', 'billing_period': 'billing_period',
    'transaction_time': 'transaction_time', 'payment_method': 'payment_method',
    **{key: key for key in ('supplier', 'invoice', 'date', 'due', 'currency', 'subtotal',
                           'tax', 'discount', 'total', 'customer', 'reference', 'type')},
}
MONEY = {'subtotal', 'tax', 'discount', 'total'}
STATES = {'resolved', 'missing', 'ambiguous', 'conflict', 'unsupported', 'unreadable'}
RESOLUTIONS = {'parser_preserved', 'candidate_for_review', 'unresolved_missing'}


def decimal_text(value, precision=14, scale=2):
    """Reject floats, malformed grouping, non-finite values and lossy NUMERIC writes."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('Financial values must be decimal strings or Decimal values.')
    text = str(value)
    if not re.fullmatch(r'-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', text):
        raise ValueError('Malformed decimal value.')
    try:
        number = Decimal(text.replace(',', ''))
        with localcontext() as context:
            context.prec = max(50, len(text) + scale)
            if (not number.is_finite() or abs(number) >= Decimal(10) ** (precision - scale)
                    or number.quantize(Decimal(10) ** -scale) != number):
                raise ValueError('Value cannot be stored exactly in the existing NUMERIC column.')
    except InvalidOperation as exc:
        raise ValueError('Malformed decimal value.') from exc
    return format(number, 'f')


def field_value(key, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('Extracted fields require nonempty strings or null.')
    if key in MONEY:
        return decimal_text(value)
    if key in {'date', 'due'}:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError('Dates require YYYY-MM-DD.')
        date.fromisoformat(value)
    if key == 'currency' and not re.fullmatch(r'[A-Z]{3}', value):
        raise ValueError('Currency requires an explicit three-letter uppercase code.')
    if key == 'transaction_time':
        if not re.fullmatch(r'[0-9]{2}:[0-9]{2}(?::[0-9]{2})?', value):
            raise ValueError('Transaction time requires HH:MM or HH:MM:SS.')
        time.fromisoformat(value)
    return value


def _cell(cell, key, *, numeric=None):
    if not isinstance(cell, dict) or cell.get('state') not in STATES:
        raise ValueError('Malformed extraction cell/state.')
    state, value = cell['state'], cell.get('value')
    if value is not None and not isinstance(value, str):
        raise ValueError('M2 cell values must be strings or null.')
    if state != 'resolved' and value is not None:
        raise ValueError('Unresolved parser fields must retain null values.')
    if state == 'resolved' and value is None:
        raise ValueError('Resolved parser fields require a value.')
    normalized = decimal_text(value, *numeric) if numeric else field_value(key, value)
    raw = cell.get('raw_text')
    evidence, candidates = deepcopy(cell.get('evidence', [])), deepcopy(cell.get('candidates', []))
    if (raw is not None and not isinstance(raw, str)) or not isinstance(evidence, list) or not isinstance(candidates, list):
        raise ValueError('Malformed raw text, evidence or candidates.')
    if any(not isinstance(item, dict) or item.get('auto_accepted') is True for item in candidates):
        raise ValueError('Candidates must remain unaccepted objects.')
    candidate = cell.get('ai_candidate')
    if candidate is not None:
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError('AI candidates require nonempty strings or null.')
        candidates.append({'source': 'ai', 'value': candidate, 'auto_accepted': False})
    resolution = cell.get('resolution')
    if resolution in {'parser_resolved', 'parser_preserved'}:
        resolution = 'parser_preserved'
    elif resolution in {None, 'unresolved', 'unresolved_missing'}:
        resolution = 'parser_preserved' if state == 'resolved' else 'unresolved_missing'
    elif resolution not in RESOLUTIONS:
        raise ValueError('Unsupported extraction resolution.')
    methods = {'parser_mapping': 'PARSER', 'PARSER': 'PARSER', 'OCR': 'OCR', 'VLM': 'VLM'}
    if cell.get('method') not in methods:
        raise ValueError('Unsupported extraction method.')
    if methods[cell['method']] == 'VLM':
        resolution = 'candidate_for_review'
        if value is not None:
            candidates.append({'source': 'ai', 'value': value, 'auto_accepted': False})
    confidence = cell.get('confidence')
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float, Decimal)):
            raise ValueError('Malformed confidence.')
        score = Decimal(str(confidence))
        if not score.is_finite() or not 0 <= score <= 1:
            raise ValueError('Confidence must be null or between zero and one.')
    return {
        'field_key': key, 'field_label': key.replace('_', ' ').title(), 'extraction_state': state,
        'resolution': resolution, 'raw_value': raw, 'normalized_value': normalized,
        'current_value': normalized if state == 'resolved' and resolution != 'candidate_for_review' else None,
        'confidence': confidence, 'evidence': evidence, 'candidates': candidates,
        'extraction_method': methods[cell['method']], 'model_name': cell.get('model_name'),
        'model_version': cell.get('model_version'), 'schema_version': VERSION,
    }


def map_m2_result(result, document_id):
    if (not isinstance(result, dict) or result.get('schema_version') != '0.1.0-proposed'
            or result.get('mode') != 'module2_public_result' or result.get('document_id') != document_id):
        raise ValueError('Unsupported M2 contract or document identity.')
    safety = result.get('safety')
    if (not isinstance(safety, dict) or result.get('review_required') is not True
            or result.get('accounting_approval_performed') is not False
            or safety.get('ai_candidate_auto_accepted') is not False
            or safety.get('automatic_accounting_approval') is not False):
        raise ValueError('M2 must require review and must never approve candidates.')
    if (not isinstance(result.get('document_type'), dict) or not isinstance(result.get('provenance'), dict)
            or result.get('status') not in {'parser_only_needs_review', 'human_review_required',
                'ai_attempt_failed_needs_review', 'candidate_for_review', 'unresolved_needs_review', 'parser_preserved'}):
        raise ValueError('Malformed M2 classification, provenance or review status.')
    if not isinstance(result.get('fields'), dict) or not isinstance(result.get('line_items'), list):
        raise ValueError('Malformed M2 fields/lines.')
    fields = {}
    for source_key, cell in result['fields'].items():
        if source_key not in FIELD_KEYS:
            raise ValueError('Unsupported extracted field: '+source_key)
        key = FIELD_KEYS[source_key]
        if key in fields:
            raise ValueError('Conflicting aliases for field: '+key)
        fields[key] = _cell(cell, key)
    lines = []
    names = {'description': 'description', 'quantity': 'quantity', 'unit_price': 'unit_price', 'line_total': 'amount'}
    for number, row in enumerate(result['line_items'], 1):
        if not isinstance(row, dict) or set(row) != set(names):
            raise ValueError('Unsupported line-item shape.')
        cells = {target: _cell(row[source], target, numeric={'quantity': (14, 3), 'unit_price': (14, 4),
                                                           'amount': (14, 2)}.get(target))
                 for source, target in names.items()}
        lines.append({
            'line_number': number, **{name: cell['current_value'] for name, cell in cells.items()},
            'field_states': {name: cell['extraction_state'] for name, cell in cells.items()},
            'evidence': {name: cell['evidence'] for name, cell in cells.items()},
            'candidates': {name: cell['candidates'] for name, cell in cells.items()},
        })
    return {'version': VERSION, 'fields': list(fields.values()), 'line_items': lines,
            'document_kind': deepcopy(result.get('document_type')),
            'category': result.get('category'), 'review_required': True}


def effective_value(row):
    """Human current values win; unresolved/candidate values never use raw fallback."""
    key = FIELD_KEYS.get(row['field_key'])
    if key is None:
        raise ValueError('Unsupported stored field: '+row['field_key'])
    corrected = row.get('corrected_at') is not None and row.get('corrected_by') is not None
    if not corrected and (row.get('extraction_state') != 'resolved' or row.get('resolution') == 'candidate_for_review'):
        return None
    value = row.get('current_value')
    if value is None and not corrected:
        value = row.get('normalized_value')
    # Raw text is evidence, not a normalized/verified accounting value.
    return field_value(key, value)


def record_values(rows):
    values = {}
    for row in rows:
        key = FIELD_KEYS.get(row['field_key'])
        if key in values:
            raise ValueError('Conflicting stored aliases for field: '+str(key))
        values[key] = effective_value(row)
    return values


def prediction_dto(row):
    return {
        'key': row['field_key'], 'label': row['field_label'], 'value': effective_value(row),
        'rawValue': row.get('raw_value'), 'normalizedValue': row.get('normalized_value'),
        'currentValue': row.get('current_value'),
        'confidence': float(row['confidence']) if row.get('confidence') is not None else None,
        'state': row.get('extraction_state'), 'resolution': row.get('resolution'),
        'evidence': row.get('evidence', []), 'candidates': row.get('candidates', []),
        'method': row.get('extraction_method'), 'modelName': row.get('model_name'),
        'modelVersion': row.get('model_version'), 'schemaVersion': row.get('schema_version'),
        'correctedBy': str(row['corrected_by']) if row.get('corrected_by') is not None else None,
        'correctedAt': str(row['corrected_at']) if row.get('corrected_at') is not None else None,
    }


def line_dto(row):
    return {'id': str(row['id']), 'description': row.get('description'),
            'quantity': decimal_text(row.get('quantity'), 14, 3),
            'unit_price': decimal_text(row.get('unit_price'), 14, 4),
            'amount': decimal_text(row.get('amount')),
            'fieldStates': row.get('field_states', {}), 'evidence': row.get('evidence', {}),
            'candidates': row.get('candidates', {})}


def json_safe(value):
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def approved_snapshot(document, extraction, fields, items):
    """Build a new snapshot only; callers must enforce actual approval gates."""
    if document.get('kind') not in {'invoice', 'bill', 'receipt', 'credit_note', 'debit_note'}:
        raise ValueError('Document kind must be explicitly confirmed.')
    if document.get('category') not in {'accounts_payable', 'accounts_receivable'}:
        raise ValueError('Business direction must be explicitly confirmed.')
    raw = extraction.get('raw_json') or {}
    if raw.get('mode') == 'module2_public_result':
        classification = raw.get('document_type') or {}
        if classification.get('status') != 'classified' or classification.get('value') != document['kind']:
            raise ValueError('Unknown or conflicting document classification requires review.')
    values = record_values(fields)
    lines = [line_dto(row) for row in items]
    if not lines or any(row[name] is None for row in lines for name in ('description', 'quantity', 'unit_price', 'amount')):
        raise ValueError('Line items must be resolved or corrected before standardisation.')
    return json_safe({'schemaVersion': VERSION, 'contentRevision': document['content_revision'],
                      'documentKind': document['kind'], 'category': document['category'],
                      'extraction': deepcopy(raw), 'reviewedFields': values,
                      'fieldPredictions': [prediction_dto(row) for row in fields], 'lineItems': lines})
