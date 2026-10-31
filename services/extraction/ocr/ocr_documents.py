"""Conservative native-OCR dispatch after the established receipt pathway.

No inference, Docling impersonation, annotation input, database or file I/O.
Reuses receipt region/field resolution and the existing labelled-map profiles.
Only explicit English invoice/receipt headings gain a new mapping path.
"""
from copy import deepcopy
from datetime import date, datetime
import re
import unicodedata

from services.extraction.contracts.accounting_contract import decimal_text, field_value
from services.extraction.classification.classify_document import heading_titles, UNSUPPORTED
from services.extraction.parsing.map_invoice import INVOICE_LABELS, RECEIPT_LABELS
from services.extraction.ocr.ocr_receipt import _Page, _lines, map_rapidocr_receipt

VERSION = 'rapidocr-document/0.1.1'
# This is an engine-text quality gate, never a field confidence/probability.
MIN_ENGINE_SCORE = .8
CURRENCY = r'RM|MYR|USD|SGD|EUR|GBP'


def _title(text):
    # Accent folding is limited to standalone headings, not names/values.
    text = ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))
    return ' '.join(text.strip(' \t-–—').casefold().split())


def _money_parts(text):
    match = re.fullmatch(r'(?P<before>(?:(?:' + CURRENCY + r')\s*)*)'
        r'(?P<amount>\d+\.\d{2})(?P<after>(?:\s*(?:' + CURRENCY + r'))*)', text, re.I)
    if not match:
        return None  # Decimal comma/localized grouping is a separate policy.
    try:
        amount = decimal_text(match['amount'])
    except ValueError:
        return None
    qualifiers = re.findall(CURRENCY, match['before'] + ' ' + match['after'], re.I)
    currencies = {'MYR' if code.upper() == 'RM' else code.upper() for code in qualifiers}
    return amount, currencies


def _money(text):
    parts = _money_parts(text)
    return parts[0] if parts is not None and len(parts[1]) <= 1 else None


def _time(text):
    try:
        return field_value('transaction_time', text)
    except ValueError:
        return None


def _date(text):
    # Only a leading complete calendar date; never infer a two-digit century
    # or choose day/month order when both numeric components are <= 12.
    match = re.match(r'^(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/?[A-Za-z]{3}/\d{4})(?=$|\s)', text)
    if not match:
        return None
    raw = match[1]
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return date.fromisoformat(raw).isoformat()
        if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', raw):
            day, month, year = map(int, raw.split('/'))
            return date(year, month, day).isoformat() if day > 12 or day == month else None
        for fmt in ('%d/%b/%Y', '%d%b/%Y'):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
    except ValueError:
        pass
    return None


def _reference(text):
    return text if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./-]{0,99}', text) else None


def _labelled(page, pattern, parse):
    candidates = page.labelled(pattern, parse)
    if parse is _money:
        # A separately boxed currency token is evidence of currency, not an
        # invalid competing monetary value. No numeric OCR region is discarded.
        candidates = [c for c in candidates if not re.fullmatch(CURRENCY, c['raw_text'], re.I)]
    return candidates


def _cell(page, candidates):
    low_quality = any(ev['engine_score'] < MIN_ENGINE_SCORE for c in candidates for ev in c['evidence'])
    return page.cell(candidates, uncertain=low_quality)


def _candidates(page, kind):
    labels = INVOICE_LABELS if kind == 'invoice' else RECEIPT_LABELS
    found = {}
    for label, key in labels.items():
        parser = (_money if key in {'subtotal', 'tax_amount', 'total_amount', 'discount'} else
                  _time if key == 'transaction_time' else
                  _date if key.endswith('_date') else _reference if key.endswith('_number') else
                  (lambda text: None if '_' in text else text) if key in {'supplier_name', 'merchant', 'buyer_name'} else
                  lambda text: text)
        found.setdefault(key, []).extend(_labelled(page, re.escape(label), parser))
    reference_key = 'invoice_number' if kind == 'invoice' else 'receipt_number'
    date_key = 'invoice_date' if kind == 'invoice' else 'transaction_date'
    found.setdefault(reference_key, []).extend(_labelled(page,
        r'(?:invoice\s+(?:no\.?|id)|bill\s+no\.?|receipt\s+(?:no\.?|reference)|CB#)', _reference))
    found.setdefault('total_amount', []).extend(_labelled(page,
        r'total\s+(?:incl\.?|inclusive)\s+(?:GST|tax)(?:\s*@\s*\d+(?:\.\d+)?%)?', _money))
    found.setdefault('tax_amount', []).extend(_labelled(page,
        r'GST(?:\s*@\s*\d+(?:\.\d+)?%)?(?:\s+included\s+in\s+total)?', _money))
    # An unlabelled date is eligible only between the heading and item region;
    # its bounds are applied by the caller after classification corroboration.
    found.setdefault(date_key, [])
    currencies = []
    for i, row in enumerate(page.rows):
        text = row['text'].strip()
        match = re.fullmatch(r'(?:currency\s*:\s*)?\(?(' + CURRENCY + r')\)?', text, re.I)
        if match:
            code = match[1].upper()
            currencies.append(page.candidate(text, 'MYR' if code == 'RM' else code, [i]))
        else:
            parts = _money_parts(text)
            if parts is not None:
                currencies.extend(page.candidate(text, code, [i]) for code in sorted(parts[1]))
            elif re.fullmatch(r'currency\s*:\s*\S+', text, re.I):
                currencies.append(page.candidate(text, None, [i]))
    # Inline labelled values (e.g. Total: MYR 12.00 USD) are not bare
    # currency rows. Preserve every qualifier of that monetary candidate;
    # never choose a first/last qualifier or inspect unrelated prose for codes.
    for key in ('subtotal', 'tax_amount', 'total_amount', 'discount'):
        for candidate in found.get(key, []):
            parts = _money_parts(candidate['raw_text'])
            if parts is not None:
                currencies.extend({**deepcopy(candidate), 'value': code} for code in sorted(parts[1]))
    # Replace raw string currency candidates with validated explicit codes.
    found['currency'] = currencies
    return found


def map_rapidocr_document(artifact, document_id, model_versions):
    original = map_rapidocr_receipt(artifact, document_id, model_versions)
    if original['document_type']['status'] != 'unknown':
        return original  # Preserve classified/ambiguous/unsupported receipt decisions.
    page = _Page(artifact, model_versions)
    headings, blockers = [], []
    for i, row in enumerate(page.rows):
        title = _title(row['text'])
        titles = heading_titles(title, leading_block=False, label='text')
        if any(t in UNSUPPORTED for t in titles):
            blockers.append(i)
        if title in {'fatura/recibo', 'factura/recibo'}:
            headings.append((i, 'mixed'))
        elif title in {'invoice', 'tax invoice', 'receipt', 'cash receipt'}:
            headings.append((i, 'invoice' if 'invoice' in title else 'receipt'))
        elif title in {'bill', 'utility bill'}:
            headings.append((i, 'bill'))  # Conflict guard only; no new bill semantics.
    if blockers or not headings:
        return original
    kinds = {kind for _, kind in headings}
    result = deepcopy(original)
    classification = {'classifier_version': VERSION, 'method': 'native_ocr_heading_rules',
        'value': 'unknown', 'status': 'unknown', 'reason': 'INSUFFICIENT_OCR_STRUCTURE',
        'candidates': [{'value': kind if kind != 'mixed' else 'unknown',
                        'matched_title': page.rows[i]['text'], 'evidence': page.evidence(i)} for i, kind in headings],
        'unsupported_headings': [], 'review_required': True}
    result['document_type'] = classification
    result['provenance']['dispatch_version'] = VERSION
    if 'mixed' in kinds or len(kinds) != 1:
        classification.update(status='ambiguous', reason='MIXED_DOCUMENT_HEADINGS')
        return result
    kind = next(iter(kinds))
    if kind not in {'invoice', 'receipt'}:
        return result
    if any(page.rows[i]['engine_score'] < MIN_ENGINE_SCORE for i, _ in headings):
        classification['reason'] = 'LOW_QUALITY_OCR_HEADING'
        return result
    if any(page.boxes[i][1] >= page.size['height'] / 2 for i, _ in headings):
        return result
    candidates = _candidates(page, kind)
    first_bottom = max(page.boxes[i][3] for i, _ in headings)
    ref_key = 'invoice_number' if kind == 'invoice' else 'receipt_number'
    date_key = 'invoice_date' if kind == 'invoice' else 'transaction_date'
    item_headers = [i for i, r in enumerate(page.rows)
                    if re.fullmatch(r'.*\b(?:qty|quantity)\b.*', r['text'], re.I)
                    and page.boxes[i][1] > first_bottom]
    item_top = min((page.boxes[i][1] for i in item_headers), default=first_bottom)
    if item_top > first_bottom:
        for i, row in enumerate(page.rows):
            box = page.boxes[i]
            # OCR rectangles can overlap a few pixels despite distinct lines.
            # Require the date centre below the heading and its whole box
            # before items; do not expand into footer/payment timestamps.
            if first_bottom < (box[1] + box[3]) / 2 and box[3] < item_top:
                value = _date(row['text'])
                if value is not None:
                    candidates[date_key].append(page.candidate(row['text'], value, [i]))
    totals = [c for c in candidates.get('total_amount', [])
              if c['value'] is not None and all(e['bbox']['t'] > first_bottom for e in c['evidence'])]
    # Corroboration uses distinct cue groups. Missing reference/date does not
    # invalidate an otherwise explicit heading plus item and amount structure.
    columns = any(page.rows[i]['engine_score'] >= MIN_ENGINE_SCORE for i in item_headers) and any(
        re.fullmatch(r'price|amount|total', r['text'], re.I) and r['engine_score'] >= MIN_ENGINE_SCORE
        and page.boxes[i][1] > first_bottom for i, r in enumerate(page.rows))
    item_math = any(re.fullmatch(r'\d+(?:\.\d+)?\s*[xX×]\s*\d+\.\d{2}', r['text'])
                    and r['engine_score'] >= MIN_ENGINE_SCORE and page.boxes[i][1] > first_bottom
                    for i, r in enumerate(page.rows))
    supported = lambda key: any(c['value'] is not None and all(e['engine_score'] >= MIN_ENGINE_SCORE for e in c['evidence'])
                                for c in candidates.get(key, []))
    strong_total = any(all(e['engine_score'] >= MIN_ENGINE_SCORE for e in c['evidence']) for c in totals)
    cues = [bool(columns or item_math), strong_total, supported(ref_key), supported(date_key)]
    if sum(cues) < 2 or not (columns or item_math):
        return result
    classification.update(value=kind, status='classified', reason='CORROBORATED_OCR_' + kind.upper())
    # Reuse the existing profile vocabulary and native region cell resolver.
    required = ({'supplier_name', 'invoice_number', 'invoice_date', 'currency', 'subtotal', 'tax_amount', 'total_amount'}
                if kind == 'invoice' else {'merchant', 'receipt_number', 'transaction_date', 'currency', 'subtotal', 'tax_amount', 'total_amount'})
    result['fields'] = {key: _cell(page, candidates.get(key, [])) for key in sorted(required | set(candidates))}
    result['line_items'] = _lines(page, totals)
    result['receipt_observations'] = {key: _cell(page, _labelled(page, label, _money))
        for key, label in [('cash_tendered', r'cash(?:\s+(?:tendered|received))?'), ('change', r'change')]}
    result['issues'].append({'code': 'OCR_PARTIAL_MAPPING',
        'detail': 'Explicit source labels only; missing identity, date, tax and item details require review. No payment state inferred.'})
    return result
