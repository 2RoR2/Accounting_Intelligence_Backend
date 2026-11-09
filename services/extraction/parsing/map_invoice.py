"""Conservative labelled-invoice mapper; standard library only. No AI calls."""
import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4
from services.extraction.classification.classify_document import classify_document
from services.extraction.contracts.accounting_contract import decimal_text, field_value
from services.extraction.layout.layout_headers import records, tax_statement
from services.extraction.layout.layout_rows import item_structure

LABELS = {
    'supplier': 'supplier_name', 'invoice number': 'invoice_number',
    'date': 'invoice_date', 'currency': 'currency', 'subtotal': 'subtotal',
    'tax amount': 'tax_amount', 'total amount': 'total_amount',
}
HEADERS = {'description': 'description', 'quantity': 'quantity',
           'unit price': 'unit_price', 'line total': 'line_total'}
AMOUNTS = {'subtotal', 'tax_amount', 'total_amount'}

# Explicit content profiles, not filename/category guesses. Optional invoice
# labels are emitted only when present, preserving the original seven fields.
INVOICE_LABELS = {**LABELS, 'vendor': 'supplier_name', 'seller': 'supplier_name',
    'invoice no': 'invoice_number', 'invoice id': 'invoice_number', 'invoice date': 'invoice_date',
    'date of issue': 'invoice_date', 'total': 'total_amount',
    'tax': 'tax_amount', 'customer': 'buyer_name', 'buyer': 'buyer_name', 'client': 'buyer_name',
    'supplier address': 'supplier_address', 'customer address': 'customer_address',
    'supplier tax id': 'supplier_tax_id', 'customer tax id': 'customer_tax_id',
    'due date': 'due_date', 'discount': 'discount',
    'allowances': 'document_allowances', 'charges': 'document_charges',
    'prepaid amount': 'prepaid_amount', 'rounding amount': 'rounding_amount',
    'total excluding tax': 'total_excluding_tax', 'total including tax': 'total_including_tax',
    'amount due': 'amount_due'}
BILL_LABELS = {'issuer': 'issuer', 'supplier': 'issuer', 'account number': 'account_number',
    'bill reference': 'bill_reference', 'bill number': 'bill_reference',
    'billing period': 'billing_period', 'bill date': 'bill_date', 'date': 'bill_date',
    'due date': 'due_date', 'currency': 'currency', 'subtotal': 'subtotal',
    'tax': 'tax_amount', 'tax amount': 'tax_amount', 'amount due': 'amount_due',
    'total': 'total_amount', 'total amount': 'total_amount',
    'current charges': 'document_charges', 'amount paid': 'amount_paid'}
RECEIPT_LABELS = {'merchant': 'merchant', 'receipt number': 'receipt_number',
    'receipt no': 'receipt_number', 'receipt no.': 'receipt_number',
    'invoice no': 'receipt_number', 'bill no': 'receipt_number',
    'transaction date': 'transaction_date', 'date': 'transaction_date',
    'transaction time': 'transaction_time', 'time': 'transaction_time',
    'currency': 'currency', 'subtotal': 'subtotal', 'tax': 'tax_amount',
    'tax amount': 'tax_amount', 'total': 'total_amount', 'total amount': 'total_amount',
    'payment method': 'payment_method'}
# Printed components of how a receipt reached its total, and its tender. Present only when printed.
RECEIPT_OPTIONAL = {'merchant_address', 'document_charges', 'rounding_amount', 'total_excluding_tax',
                    'total_including_tax', 'tendered_amount', 'change_given'}
NOTE_LABELS = {'supplier': 'supplier_name', 'issuer': 'supplier_name',
    'credit note number': 'credit_note_number', 'debit note number': 'debit_note_number',
    'reference': 'reference_number', 'original invoice': 'original_invoice_number',
    'invoice number': 'original_invoice_number', 'date': 'invoice_date',
    'currency': 'currency', 'subtotal': 'subtotal', 'tax': 'tax_amount',
    'tax amount': 'tax_amount', 'total': 'total_amount', 'total amount': 'total_amount'}
PROFILE_FIELDS = {'invoice': set(LABELS.values()), 'bill': set(BILL_LABELS.values()),
                  'receipt': set(RECEIPT_LABELS.values()), 'unknown': set()}
PROFILE_FIELDS.update({'credit_note': set(NOTE_LABELS.values()), 'debit_note': set(NOTE_LABELS.values())})
INVOICE_OPTIONAL = {'buyer_name', 'due_date', 'discount', 'document_allowances', 'document_charges',
                    'prepaid_amount', 'rounding_amount', 'total_excluding_tax',
                    'total_including_tax', 'amount_due', 'supplier_address', 'customer_address',
                    'supplier_tax_id', 'customer_tax_id'}
BILL_OPTIONAL = {'customer_name', 'customer_address', 'provider_address', 'previous_balance', 'arrears',
                 'adjustment_total', 'credit_total', 'late_fee'}


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


def _column_account_number(document):
    """One identifier printed directly beneath an "Account No." column heading."""
    positioned = []
    for item in records(document):
        prov = item.get('prov') or []
        if len(prov) != 1:
            continue
        ev = location(document, prov[0].get('page_no'), prov[0].get('bbox'), item.get('self_ref'), item.get('text', ''))
        if ev.get('bbox'):
            ev.update(from_ocr=item.get('from_ocr'), ocr_score=item.get('ocr_score'), bbox_scope='source_line')
            positioned.append((item.get('text', '').strip(), ev))
    for text, label in positioned:
        if not re.fullmatch(r'account\s*(?:no\.?|number)\s*:?', text, re.I):
            continue
        a = label['bbox']; height = a['b'] - a['t']
        values = [(value, ev) for value, ev in positioned if ev['page'] == label['page']
                  and 0 <= ev['bbox']['t'] - a['b'] <= height * 1.8 and abs(ev['bbox']['l'] - a['l']) <= height * 2
                  and re.fullmatch(r'(?=[A-Za-z0-9./_-]*\d)[A-Za-z0-9][A-Za-z0-9./_-]{3,63}', value)]
        if len(values) == 1:
            value, ev = values[0]
            evidence = [label, ev]
            return mapped_field(text + '\n' + value, value, evidence, 'mapped',
                                [{'raw_text': text + '\n' + value, 'value': value, 'evidence': evidence}])
    return None


# Any of these below an aggregate, or as a column, can change what "Net worth" means.
_NET_WORTH_BLOCKERS = re.compile(r'\bdisc\.|\b(?:sub\s*-?\s*total|discounts?|allowances?|surcharges?|charges?|shipping|'
                                 r'freight|rounding|prepaid|prepayment|deposit|adjustments?|credits?)\b', re.I)


PRETAX_STRUCTURE_VERSION = 'pretax-structure/1'
_TAX_EXCLUSIVE_LABEL = re.compile(r'net\s+(?:worth|amount|total)\b|total\s+before\s+tax\b|total\s+excl(?:uding|\.)?\s+tax\b', re.I)
_ADJUSTMENT_FIELDS = ('discount', 'document_allowances', 'document_charges', 'prepaid_amount', 'rounding_amount')


def pretax_structure(document, fields, rows):
    """Source facts about a printed tax-exclusive total. No arithmetic, no decision.

    subtotal and total_excluding_tax stay distinct fields. Module 3 decides, from
    these facts and the current values, whether the printed tax-exclusive total can
    serve as its pre-tax basis. Any blocker means that use is not established.
    """
    subtotal, net = fields.get('subtotal'), fields.get('total_excluding_tax')
    result = {'version': PRETAX_STRUCTURE_VERSION, 'eligible': False, 'blockers': [],
              'printed_label': None, 'source_refs': []}
    blockers = result['blockers']
    if not net or net['mapping_status'] != 'mapped' or net['value'] is None or not net['candidates']:
        blockers.append('NO_RESOLVED_TAX_EXCLUSIVE_TOTAL')
        return result
    if not subtotal or subtotal['value'] is not None or any(c.get('value') for c in subtotal['candidates']):
        blockers.append('PRINTED_SUBTOTAL_PRESENT')
    entries = [e for c in net['candidates'] for e in c['evidence']]
    labelled = [(match, e) for e in entries for match in [_TAX_EXCLUSIVE_LABEL.match(e.get('text', '').strip())] if match]
    if not labelled:
        blockers.append('TAX_EXCLUSIVE_LABEL_NOT_RECOGNISED')
    if any(not e.get('bbox') or not e.get('page') for e in entries):
        blockers.append('INCOMPLETE_SOURCE_GEOMETRY')
    try:
        if len({Decimal(c['value']) for c in net['candidates']}) != 1:
            blockers.append('CONFLICTING_TAX_EXCLUSIVE_VALUES')
    except (InvalidOperation, TypeError, ValueError):
        blockers.append('UNSUPPORTED_TAX_EXCLUSIVE_VALUE')
    for key in _ADJUSTMENT_FIELDS:
        if fields.get(key) and (fields[key]['value'] is not None or fields[key]['candidates']):
            blockers.append('ADJUSTMENT_FIELD:' + key)
    if not rows:
        blockers.append('NO_LINE_ITEM_EVIDENCE')
    for row in rows:
        cell = row['line_total']
        if (cell['mapping_status'] != 'mapped' or cell['value'] is None or not any(
                e.get('amount_basis') == 'net_excluding_tax' and e.get('bbox') for e in cell['evidence'])):
            blockers.append('LINE_AMOUNTS_NOT_ON_NET_BASIS')
            break
    if labelled:
        match, source = labelled[0]
        result['printed_label'] = match[0]
        result['source_refs'] = sorted({e['source_ref'] for e in entries if e.get('source_ref')})
        page, top, height = source['page'], source['bbox']['t'], source['bbox']['b'] - source['bbox']['t']
        rates = 0
        for item in records(document):
            prov = (item.get('prov') or [{}])[0]
            box = location(document, prov.get('page_no'), prov.get('bbox'), None, '').get('bbox')
            if not box or prov.get('page_no') != page:
                continue
            text = item.get('text', '').strip()
            # In or below the summary, or a short column-style label anywhere.
            if _NET_WORTH_BLOCKERS.search(text) and (box['t'] >= top - height * 2 or len(text) <= 24):
                if 'ADJUSTMENT_TEXT_PRINTED' not in blockers:
                    blockers.append('ADJUSTMENT_TEXT_PRINTED')
            if box['t'] >= top and re.fullmatch(r'\d+(?:[.,]\d+)?\s*%', text):
                rates += 1
        if rates > 1:
            blockers.append('MULTIPLE_TAX_RATE_ROWS')
    result['eligible'] = not blockers
    return result


def attach_net_worth_candidate(fields, rows, structure):
    """Offer the printed tax-exclusive total as an unselected subtotal candidate.

    field_dictionary 1.0.0 scopes subtotal to its printed label, so this never
    resolves subtotal: its value stays null and only a person can confirm it. The
    comparisons below only gate the offer; nothing here computes a value.
    """
    if not structure['eligible']:
        return
    net, subtotal = fields['total_excluding_tax'], fields['subtotal']
    tax, total = fields.get('tax_amount'), fields.get('total_amount')
    if any(f is None or f['mapping_status'] != 'mapped' or f['value'] is None for f in (tax, total)):
        return
    try:
        printed = Decimal(net['value'])
        if (sum((Decimal(row['line_total']['value']) for row in rows), Decimal('0')) != printed
                or printed + Decimal(tax['value']) != Decimal(total['value'])):
            return
    except (InvalidOperation, TypeError, ValueError):
        return
    evidence = deepcopy(net['candidates'][0]['evidence'])
    for entry in evidence:
        entry.update(printed_label=structure['printed_label'], printed_value=evidence[-1].get('text'),
                     extraction_method='OCR' if entry.get('from_ocr') is True else 'PARSER',
                     candidate_basis='printed_tax_exclusive_total')
    subtotal['candidates'] = [{'raw_text': net['candidates'][0]['raw_text'], 'value': format(printed, 'f'), 'evidence': evidence}]


def with_recovery_placeholders(result):
    """Empty cells for recoverable fields, so a person or model can fill what the parser never saw."""
    from services.extraction.classification.route_extraction import RECOVERY_FIELDS
    profile = result['provenance'].get('document_profile')
    for key in RECOVERY_FIELDS.get(profile, ()):
        result['fields'].setdefault(key, mapped_field())
    if profile == 'receipt':
        # OCR can miss these rows. A person needs somewhere to enter what is printed, or a correct total can never reconcile.
        for key in ('total_excluding_tax', 'total_including_tax', 'rounding_amount'):
            result['fields'].setdefault(key, mapped_field())
    return result


def _with_bill_components(result, document):
    """Printed balance, payment, credit and fee rows, so amount due can be reconciled from the source."""
    from services.extraction.layout.layout_headers import bill_components
    for key, (raw, value, evidence) in bill_components(document, location).items():
        if (result['fields'].get(key) or {}).get('value') is None:
            result['fields'][key] = mapped_field(raw, value, evidence, 'mapped', [{'raw_text': raw, 'value': value, 'evidence': evidence}])
            result['issues'] = [i for i in result['issues'] if not (i['code'] == 'UNMAPPED_FIELD' and i['detail'].startswith(key + ':'))]
    return result


def map_document(document):
    if document.get('schema_name') != 'DoclingDocument':
        raise ValueError('Expected a DoclingDocument JSON export')
    classification = classify_document(document)
    if classification['value'] == 'unknown' and classification['status'] != 'unsupported':
        # Reuse the labelled header/row mapper, without asserting an invoice
        # type. Its vocabulary is only a container for provisional source reads.
        result = _map_document(document, classification, 'invoice')
        result['fields'] = {key: cell for key, cell in result['fields'].items()
                            if cell['evidence'] or cell['candidates']}
        if result['fields'] or result['line_items']:
            for cell in list(result['fields'].values()) + [
                    cell for row in result['line_items'] for cell in row.values()]:
                if cell['value'] is not None and not any(
                        c['value'] == cell['value'] for c in cell['candidates']):
                    cell['candidates'].append({'raw_text': cell['raw_text'], 'value': cell['value'],
                                               'evidence': deepcopy(cell['evidence'])})
                for ev in cell['evidence'] + [e for c in cell['candidates'] for e in c['evidence']]:
                    ev['candidate_basis'] = 'provisional_document_type'
                if cell['value'] is not None:
                    cell['value'], cell['mapping_status'] = None, 'ambiguous'
            result['provenance'].update(document_profile='unknown',
                                        provisional_fields=True, source_layout_profile='invoice')
            result['issues'].append({'code': 'PROVISIONAL_DOCUMENT_FIELDS',
                'detail': 'Source reads retained as candidates; document type and field roles require review.'})
            return result
        # Documents with no supported source reads retain the original empty shape.
        return _map_document(document, classification, 'unknown')
    if (classification['value'] == 'bill'
            and classification['reason'] == 'UTILITY_USAGE_AND_BILLING_EVIDENCE'):
        # Utility statements can use an invoice form. Reuse its established
        # source-backed layout rules, then expose the existing bill vocabulary.
        # No amount_due/total/current_charges or payment substitution is made.
        result = _map_document(document, classification, 'invoice')
        original = result['fields']
        names = {'supplier_name': 'issuer', 'invoice_number': 'bill_reference',
                 'invoice_date': 'bill_date', 'buyer_name': 'customer_name',
                 'supplier_address': 'provider_address'}
        allowed = (PROFILE_FIELDS['bill'] - {'document_charges', 'amount_paid'}) | BILL_OPTIONAL
        result['fields'] = {names.get(key, key): value for key, value in original.items()
                            if names.get(key, key) in allowed}
        for key in PROFILE_FIELDS['bill'] - set(result['fields']):
            result['fields'][key] = mapped_field()
        # Bills have no tax-exclusive field in their vocabulary. A printed "Net Total"
        # is their subtotal in the published labels; keep its evidence and raw text.
        net, subtotal = original.get('total_excluding_tax'), result['fields']['subtotal']
        if (net and net['value'] is not None and subtotal['value'] is None and net['candidates']
                and all(re.match(r'net\s+total\b', c['raw_text'] or '', re.I) for c in net['candidates'])):
            result['fields']['subtotal'] = deepcopy(net)
            result['issues'] = [i for i in result['issues']
                                if not (i['code'] == 'UNMAPPED_FIELD' and i['detail'].startswith('subtotal:'))]
        if result['fields']['account_number']['value'] is None:
            account = _column_account_number(document)
            if account:
                result['fields']['account_number'] = account
                result['issues'] = [i for i in result['issues']
                                    if not (i['code'] == 'UNMAPPED_FIELD' and i['detail'].startswith('account_number:'))]
        result['provenance'].update(document_profile='bill', source_layout_profile='invoice',
                                    source_layout_fields=deepcopy(original))
        return _with_bill_components(result, document)
    result = _map_document(document, classification, classification['value'])
    return _with_bill_components(result, document) if classification['value'] == 'bill' else result


def _map_document(document, classification, kind):
    issues, found = [], defaultdict(list)
    heading_blocks = {(c['evidence']['source_ref'], c['evidence']['text'])
                      for c in classification['candidates'] if c['value'] == kind}
    ambiguous_boundaries = set()
    labels = {'invoice': INVOICE_LABELS, 'bill': BILL_LABELS,
              'receipt': RECEIPT_LABELS, 'credit_note': NOTE_LABELS,
              'debit_note': NOTE_LABELS}.get(kind, {})
    if kind == 'receipt':
        labels = {**labels, 'merchant address': 'merchant_address'}
    boundaries = set(labels) | ({'bill to'} if kind == 'invoice' else set())
    if kind == 'bill':
        boundaries |= {'previous balance', 'balance brought forward'}
    label_pattern = re.compile(r'(?<!\S)(?P<label>' + '|'.join(
        re.escape(label).replace(r'\ ', r'\s+') for label in sorted(boundaries, key=len, reverse=True)
    ) + r')\s*:\s*', re.IGNORECASE) if labels else None

    def issue(code, detail):
        issues.append({'code': code, 'detail': detail})

    for index, item in enumerate(records(document)):
        if item.get('content_layer', 'body') != 'body':
            continue
        text = item.get('text', '')
        # Docling can merge separate labelled PDF lines into one text block.
        # Split only explicit known label boundaries, never dates/time colons or
        # unlabeled prose; evidence keeps the full block and exact character span.
        matches = list(label_pattern.finditer(text)) if label_pattern else []
        if not matches:
            continue
        ref = item.get('self_ref', f'#/texts/{index}')
        # Only the actual classified heading block may have an unlabelled prefix.
        # Other prose/descriptions keep the original start-of-block guard.
        if text[:matches[0].start()].strip() and (ref, text) not in heading_blocks:
            continue
        for mi, match in enumerate(matches):
            key = labels.get(' '.join(match['label'].casefold().split()))
            if key is None:  # Recognized delimiter, not an accounting assertion.
                continue
            end = matches[mi + 1].start() if mi + 1 < len(matches) else len(text)
            raw = text[match.start():end].strip()
            value = text[match.end():end].strip()
            if mi:
                previous = matches[mi - 1]
                previous_key = labels.get(' '.join(previous['label'].casefold().split()))
                if previous_key in {'supplier_name', 'issuer', 'merchant'}:
                    previous_value = text[previous.end():match.start()].strip()
                    # A colon-labelled fragment may itself be part of a name.
                    # Do not resolve a truncated name or a label with no prior value.
                    typed_valid = True
                    try:
                        if key in AMOUNTS | {'amount_due', 'discount'}:
                            typed_valid = re.fullmatch(r'(?:[A-Z]{3}\s+)?-?\d+(?:\.\d+)?', value) is not None
                        elif key in {'invoice_date', 'bill_date', 'transaction_date', 'due_date', 'currency'}:
                            field_value('date' if key.endswith('_date') else key, value)
                    except ValueError:
                        typed_valid = False
                    adjacent_party_labels = (not previous_value and kind == 'invoice'
                                             and key in {'supplier_name', 'buyer_name'})
                    if (not previous_value or not typed_valid) and not adjacent_party_labels:
                        ambiguous_boundaries.add(previous_key)
                        if not previous_value:
                            ambiguous_boundaries.add(key)
                        issue('AMBIGUOUS_LABEL_BOUNDARY', previous_key + ': possible label inside source name')
            evidence = [location(document, p.get('page_no'), p.get('bbox'),
                        item.get('self_ref', f'#/texts/{index}'), text)
                        for p in item.get('prov', [])]
            for ev in evidence:
                ev['text_span'] = [match.start(), end]
                ev['bbox_scope'] = 'source_block'
                ev['from_ocr'] = item.get('from_ocr')
                ev['ocr_score'] = item.get('ocr_score')
            found[key].append({'raw_text': raw, 'value': value,
                               'evidence': evidence})

    from services.extraction.layout.layout_headers import recover_headers
    recover_headers(document, kind, labels, found, location)
    if kind == 'receipt':
        from services.extraction.layout.layout_headers import drop_tax_summary_amounts
        drop_tax_summary_amounts(document, found, location)

    fields = {}
    for key in sorted(PROFILE_FIELDS[kind] | set(found)):
        candidates = found[key]
        values = {c['value'] for c in candidates}
        raw = '\n'.join(c['raw_text'] for c in candidates) or None
        candidate_evidence = [e for c in candidates for e in c['evidence']]
        if key in ambiguous_boundaries:
            fields[key] = mapped_field(raw=raw, evidence=candidate_evidence, state='ambiguous', candidates=candidates)
        elif key == 'currency' and values == {None}:
            # Printed `$` only: source-backed, but it does not identify a currency.
            fields[key] = mapped_field(raw=raw, evidence=candidate_evidence, state='ambiguous', candidates=candidates)
            issue('AMBIGUOUS_CURRENCY_SYMBOL', 'currency: a printed $ alone does not identify a currency')
        elif not candidates or values == {''}:
            fields[key] = mapped_field(candidates=candidates)
            issue('UNMAPPED_FIELD', key + ': no non-empty supported label/value found')
        elif len(values) != 1:
            fields[key] = mapped_field(raw=raw, evidence=candidate_evidence, state='ambiguous', candidates=candidates)
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
    money_fields = AMOUNTS | {'discount', 'amount_due', 'document_charges', 'amount_paid',
                             'total_excluding_tax', 'total_including_tax', 'document_allowances',
                             'prepaid_amount', 'rounding_amount', 'tendered_amount', 'change_given'}
    for key in money_fields:
        if key not in fields:
            continue
        f = fields[key]
        if f['value'] is None:
            continue
        match = re.fullmatch(r'(?:(?P<currency>[A-Z]{3})\s+)?(?P<amount>-?\d+(?:\.\d+)?)', f['value'])
        if not match or (match['currency'] and match['currency'] != fields['currency']['value']):
            f['value'], f['mapping_status'] = None, 'ambiguous'
            issue('AMOUNT_REQUIRES_REVIEW', key + ': unsupported format or currency disagreement')
        else:
            f['value'] = match['amount']

    # Unsupported formats stay visible as candidates, never fail the whole job
    # or enter a lossy NUMERIC/date write. Raw strings/evidence remain unchanged.
    typed_keys = {'invoice_date': 'date', 'bill_date': 'date', 'transaction_date': 'date',
                  'due_date': 'due', 'currency': 'currency', 'transaction_time': 'transaction_time'}
    for key, f in fields.items():
        if f['value'] is None:
            continue
        try:
            if key in money_fields:
                f['value'] = decimal_text(f['value'])
            elif key in typed_keys:
                f['value'] = field_value(typed_keys[key], f['value'])
        except ValueError:
            f['value'], f['mapping_status'] = None, 'ambiguous'
            issue('FORMAT_REQUIRES_REVIEW', key)

    rows = []
    for ti, table in enumerate(document.get('tables', []) if kind not in {'unknown', 'invoice'} else []):
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
            # Docling can omit the header flag for one cell in a short charges
            # table. Require explicit supported text in every first-row cell.
            if c.get('start_row_offset_idx') == 0 or c.get('column_header'):
                name = HEADERS.get(' '.join(c.get('text', '').casefold().split()))
                col = c.get('start_col_offset_idx')
                if c.get('start_row_offset_idx') != 0 or not name or col in columns or name in columns.values():
                    bad_header = True
                columns[col] = name
        if (bad_header or not {'description', 'line_total'} <= set(columns.values())
                or data.get('num_cols') != len(columns) or len(columns) not in {2, 3, 4}):
            issue('UNSUPPORTED_TABLE', ref + ': expected unique description/line total and optional quantity/unit price headers')
            continue
        row_count = data.get('num_rows', 0)
        if not isinstance(row_count, int) or row_count < 2:
            issue('UNSUPPORTED_TABLE', ref + ': no data rows')
            continue
        grouped = defaultdict(list)
        for ci, cell in enumerate(cells):
            if cell.get('start_row_offset_idx') != 0:
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
                if value is not None and key != 'description':
                    try:
                        row[key]['value'] = decimal_text(value, 14, {'quantity': 3, 'unit_price': 4, 'line_total': 2}[key])
                    except ValueError:
                        row[key]['value'], row[key]['mapping_status'] = None, 'ambiguous'
                        issue('FORMAT_REQUIRES_REVIEW', f'{ref} row {ri} field {key}')
                if not value or ev['bbox'] is None:
                    issue('INCOMPLETE_TABLE_EVIDENCE', f'{ref} row {ri} field {key}')
            for key in set(HEADERS.values()) - set(row):
                row[key] = mapped_field()
            rows.append(row)
    if not rows:
        from services.extraction.layout.layout_rows import recover_rows
        rows = recover_rows(document, kind, fields.get('currency', {}).get('value'),
                            location, mapped_field, issue)
    if not rows and kind in {'invoice', 'receipt'}:
        from services.extraction.layout.layout_rows import recover_stacked_rows
        try:
            rows = recover_stacked_rows(document, fields.get('currency', {}).get('value'), location, mapped_field, issue)
        except (ValueError, ArithmeticError):
            rows = []
    if not rows and (kind == 'receipt' or classification['value'] == 'unknown'):
        from services.extraction.layout.layout_rows import recover_receipt_rows
        try:
            rows = recover_receipt_rows(document, fields.get('currency', {}).get('value'), location, mapped_field, issue)
        except (ValueError, ArithmeticError):
            rows = []
        if rows:
            # These rows passed a printed-total check, so an unparsed Docling table no longer hides anything.
            issues[:] = [item for item in issues if item.get('code') != 'UNSUPPORTED_TABLE']
    if not rows:
        issue('NO_MAPPED_LINE_ITEMS', 'No supported line-item rows mapped; inspect original tables')
    structure = None
    if kind == 'invoice' and classification['value'] == 'invoice':
        structure = pretax_structure(document, fields, rows)
        attach_net_worth_candidate(fields, rows, structure)
    issue('HUMAN_REVIEW_REQUIRED', 'Proposed extraction only; no accounting validation or approval performed')
    return {'schema_version': 'draft-0.2', 'mode': 'real_parser_mapping',
            'document_id': document.get('origin', {}).get('filename') or document.get('name'),
            'status': 'needs_review', 'fields': fields, 'line_items': rows,
            'issues': issues, 'provenance': {'path': 'docling_explicit_mapping',
            'mapper_version': '0.8.0', 'document_profile': kind, 'model': None,
            **({'pretax_structure': structure} if structure else {}),
            'item_structure': item_structure(document, rows), 'tax_statement': tax_statement(document),
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
