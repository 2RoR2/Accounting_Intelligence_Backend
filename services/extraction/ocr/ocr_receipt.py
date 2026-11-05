"""Offline, single-page RapidOCR receipt adapter. No Docling, inference or I/O.

Consumes native region output, not SROIE transcripts. Returns the existing M2
public/accounting boundary; cash/change are noncanonical review observations.
Used by the opt-in image parser; inference and persistence remain separate.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
import re

from services.extraction.contracts.accounting_contract import decimal_text
from services.extraction.classification.classify_document import heading_titles, UNSUPPORTED
from services.extraction.contracts.module2_public_result import public_field

VERSION = 'rapidocr-receipt/0.2'
# This receipt-only adapter has no credit/debit mapping profile. The main
# Docling classifier continues supporting those kinds on current features.
RECEIPT_UNSUPPORTED = UNSUPPORTED | {'credit note', 'credit memo', 'debit note', 'debit memo'}


def _amount(text):
    if re.fullmatch(r'(?:RM\s*|MYR\s*)?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\.[0-9]{2}', text):
        try:
            return decimal_text(re.sub(r'^(?:RM|MYR)\s*', '', text))
        except ValueError:
            return None
    return None


def _date(text):
    match = re.fullmatch(r'([0-9]{2}/[0-9]{2}/[0-9]{4})(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\s*(?:AM|PM)?)?', text, re.I)
    if match:
        try:
            return datetime.strptime(match[1], '%d/%m/%Y').date().isoformat()
        except ValueError:
            pass
    return None


def _reference(text):
    return text if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./-]*', text) else None


class _Page:
    def __init__(self, artifact, versions):
        if (artifact.get('status') != 'PASS' or artifact.get('annotation_input') is not False
                or artifact.get('method') != 'direct RapidOCR; not Docling pipeline'
                or artifact.get('device') != 'cpu'):
            raise ValueError('Expected successful native CPU RapidOCR output, not annotations or Docling.')
        if not re.fullmatch(r'[0-9a-f]{64}', artifact.get('image_sha256', '')):
            raise ValueError('Missing source image identity.')
        if not all(isinstance(versions.get(k), str) and versions[k].strip()
                   for k in ('rapidocr', 'onnxruntime')):
            raise ValueError('Recorded OCR/runtime versions required.')
        size = artifact.get('image_size', {})
        if not all(type(size.get(k)) is int and 0 < size[k] <= 100000 for k in ('width', 'height')):
            raise ValueError('Invalid source image dimensions.')
        rows = artifact.get('rows')
        if not isinstance(rows, list) or not 0 < len(rows) <= 1000:
            raise ValueError('Expected bounded OCR regions.')
        self.rows, self.boxes, self.size, self.versions = deepcopy(rows), [], size, versions
        for index, row in enumerate(self.rows):
            if (type(row.get('order')) is not int or row['order'] != index
                    or not isinstance(row.get('text'), str) or not 0 < len(row['text']) <= 2000):
                raise ValueError('Invalid OCR text/order.')
            polygon = row.get('box_polygon_pixels')
            if not isinstance(polygon, list) or len(polygon) != 4:
                raise ValueError('Expected quadrilateral OCR box.')
            for point in polygon:
                if (not isinstance(point, list) or len(point) != 2
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)
                        or not 0 <= point[0] <= size['width'] or not 0 <= point[1] <= size['height']):
                    raise ValueError('Invalid OCR coordinates.')
            xs, ys = zip(*polygon)
            box = (min(xs), min(ys), max(xs), max(ys))
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ValueError('Degenerate OCR box.')
            score = row.get('engine_score')
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('Invalid engine score; not field confidence.')
            self.boxes.append(box)

    def evidence(self, index):
        row, (l, t, r, b) = self.rows[index], self.boxes[index]
        return {'source_ref': f'#/rows/{index}', 'page': 1, 'text': row['text'],
                'region_order': row['order'], 'bbox_original': deepcopy(row['box_polygon_pixels']),
                'bbox': {'l': l, 't': t, 'r': r, 'b': b, 'coord_origin': 'TOPLEFT'},
                'page_size': deepcopy(self.size), 'coordinate_units': 'pixels',
                'engine_score': row['engine_score']}

    def aligned(self, a, b):
        x, y = self.boxes[a], self.boxes[b]
        ha, hb = x[3] - x[1], y[3] - y[1]
        return (max(ha, hb) <= 1.6 * min(ha, hb)
                and min(x[3], y[3]) - max(x[1], y[1]) >= .6 * min(ha, hb))

    def labelled(self, label, parse, *, leaders=False):
        found = []
        for i, row in enumerate(self.rows):
            separator = r'(?::|[.\u2026]+)' if leaders else ':'
            match = re.fullmatch(r'\s*(?:' + label + r')\s*(?:' + separator + r'\s*(.*))?\s*', row['text'], re.I)
            if not match:
                continue
            if match[1] and match[1].strip():
                raw = match[1].strip()
                found.append(self.candidate(raw, parse(raw), [i]))
            else:
                box = self.boxes[i]
                for j, other in enumerate(self.rows):
                    if self.boxes[j][0] >= box[2] and self.aligned(i, j):
                        raw = other['text'].strip()
                        found.append(self.candidate(raw, parse(raw), [i, j]))
        return found

    def candidate(self, raw, value, indices):
        return {'raw_text': raw, 'value': value, 'evidence': [self.evidence(i) for i in indices]}

    def cell(self, candidates, *, uncertain=False):
        values = {c['value'] for c in candidates if c['value'] is not None}
        if not candidates:
            state = 'missing'
        elif len(values) > 1:
            state = 'conflict'
        elif uncertain or len(values) != 1 or any(c['value'] is None for c in candidates):
            state = 'ambiguous'
        else:
            state = 'resolved'
        base = {'value': next(iter(values)) if state == 'resolved' else None,
                'state': state, 'method': 'OCR', 'review_required': True,
                'raw_text': '\n'.join(c['raw_text'] for c in candidates) if candidates else None,
                'evidence': [deepcopy(e) for c in candidates for e in c['evidence']],
                'candidates': deepcopy(candidates)}
        return {**public_field(base), 'confidence': None, 'model_name': 'RapidOCR',
                'model_version': self.versions['rapidocr']}

    def indices(self, pattern):
        return [i for i, r in enumerate(self.rows) if re.fullmatch(pattern, r['text'].strip(), re.I)]


def _ordinary_layout(page, totals):
    """A standalone upper-half receipt title, one aligned item header and a
    calendar date in between, followed by an explicitly labelled money summary.
    This establishes document kind, not merchant identity or amount correctness.
    """
    columns = [page.indices(p) for p in (r'DESC(?:/ITEM|RIPTION)?', r'QTY|QUANTITY',
               r'PRICE', r'(?:AMOUNT|AMT)(?:\s*\((?:RM|MYR|USD|SGD|EUR|GBP)\))?')]
    if any(len(c) != 1 for c in columns):
        return [], []
    columns = [c[0] for c in columns]
    if (not all(page.aligned(columns[0], j) for j in columns[1:])
            or [page.boxes[j][0] for j in columns] != sorted(page.boxes[j][0] for j in columns)):
        return [], []
    top = min(page.boxes[j][1] for j in columns)
    bottom = max(page.boxes[j][3] for j in columns)
    summaries = [c for c in totals if c['value'] is not None
                 and all(e['bbox']['t'] > bottom for e in c['evidence'])]
    if not summaries:
        return [], []
    headings, dates = [], []
    for i in page.indices(r'RECEIPT'):
        box = page.boxes[i]
        if not box[1] < page.size['height'] / 2 or not box[3] < top:
            continue
        header_dates = []
        for j, row in enumerate(page.rows):
            if not box[3] < page.boxes[j][1] < page.boxes[j][3] < top:
                continue
            # A trailing till/counter marker is not part of a date or reference.
            value = _date(re.sub(r'\s+#[0-9]+$', '', row['text'].strip()))
            if value is not None:
                header_dates.append(page.candidate(row['text'], value, [j]))
        if header_dates:
            headings.append(i)
            dates.extend(header_dates)
    return headings, dates


def _classification(page, date, reference, total, cash, ordinary=()):
    titles = [(i, t) for i, row in enumerate(page.rows)
              for t in heading_titles(row['text'], leading_block=False, label='text')]
    excluded = [{'matched_title': t, 'evidence': page.evidence(i)} for i, t in titles if t in RECEIPT_UNSUPPORTED]
    headings = page.indices(r'CASH\s+BILL')
    # Require a standalone heading followed by retail columns, then total and
    # cash payment. A prose occurrence or unlabeled number cannot classify.
    corroborated = []
    for i in headings:
        start = page.boxes[i][3]
        columns = [page.indices(p) for p in (r'PRICE', r'AMOUNT', r'QTY')]
        if not all(columns) or not all(c['value'] is not None for c in date + reference + total + cash):
            continue
        if not all((date, reference, total, cash)):
            continue
        total_top = min(e['bbox']['t'] for c in total for e in c['evidence'])
        cash_top = min(e['bbox']['t'] for c in cash for e in c['evidence'])
        if all(any(start < page.boxes[j][1] < total_top for j in col) for col in columns) and total_top < cash_top:
            corroborated.append(i)
    # The corroborated RECEIPT title is expected; any other title still vetoes
    # or makes competing supported document kinds ambiguous.
    competing = any(i not in ordinary for i, _ in titles) or bool(corroborated and ordinary)
    matched = corroborated + list(ordinary)
    status = ('unsupported' if excluded else 'ambiguous' if matched and competing
              else 'classified' if matched else 'unknown')
    return {'classifier_version': VERSION, 'method': 'ocr_receipt_heading_rules',
            'value': 'receipt' if status == 'classified' else 'unknown', 'status': status,
            'reason': ('CORROBORATED_CASH_BILL' if corroborated else 'CORROBORATED_RECEIPT')
                      if status == 'classified' else 'RECEIPT_REQUIRES_REVIEW',
            'candidates': [{'value': 'receipt', 'matched_title': 'cash bill' if i in corroborated else 'receipt',
                            'evidence': page.evidence(i)} for i in matched],
            'unsupported_headings': excluded, 'review_required': True}


def _lines(page, total):
    columns = [page.indices(p) for p in (r'PRICE', r'AMOUNT', r'QTY', r'(?:CODE/)?DESC(?:RIPTION)?')]
    if any(len(c) != 1 for c in columns) or not total:
        return []
    price_col, amount_col, qty_col, desc_col = [c[0] for c in columns]
    top = max(page.boxes[c][3] for c in (price_col, amount_col, qty_col, desc_col))
    bottom = min(e['bbox']['t'] for c in total for e in c['evidence'])
    result = []
    for i, row in enumerate(page.rows):
        match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s+(?:PC|PCS|EA|UNIT|UNITS)', row['text'], re.I)
        if not match or not top < page.boxes[i][1] < bottom:
            continue
        qbox = page.boxes[qty_col]
        cx = (page.boxes[i][0] + page.boxes[i][2]) / 2
        if not qbox[0] <= cx <= qbox[2] + (qbox[3] - qbox[1]):
            continue
        try:
            quantity = decimal_text(match[1], 14, 3)
        except ValueError:
            continue
        if Decimal(quantity) <= 0:
            continue
        fields = {'quantity': page.cell([page.candidate(row['text'], quantity, [qty_col, i])])}
        for name, column, scale in [('unit_price', price_col, 4), ('line_total', amount_col, 2)]:
            candidates = []
            for j, other in enumerate(page.rows):
                cb, vb = page.boxes[column], page.boxes[j]
                center = (vb[0] + vb[2]) / 2
                if j != i and page.aligned(i, j) and cb[0] <= center <= cb[2] + (cb[3] - cb[1]):
                    raw = other['text'].strip()
                    try:
                        value = decimal_text(raw, 14, scale) if re.fullmatch(r'[0-9]+\.[0-9]{2,4}', raw) else None
                    except ValueError:
                        value = None
                    candidates.append(page.candidate(raw, value, [column, j]))
            fields[name] = page.cell(candidates)
        descriptions = []
        for j, other in enumerate(page.rows):
            b, q = page.boxes[j], page.boxes[i]
            if (top < b[1] < b[3] <= q[1] and q[1] - b[3] <= 2 * (q[3] - q[1])
                    and len(re.findall(r'[A-Za-z]+', other['text'])) >= 2):
                descriptions.append(page.candidate(other['text'], other['text'], [desc_col, j]))
        fields['description'] = page.cell(descriptions)
        result.append(fields)
    return result


def map_rapidocr_receipt(artifact, document_id, model_versions):
    """Map one native image OCR result to M2 without pretending it is Docling.

    `model_versions` must come from the recorded inference environment. Engine
    scores are preserved as region metadata, never promoted to field confidence.
    No manual reference/annotation input is accepted by this function.
    """
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError('Document identity required.')
    page = _Page(artifact, model_versions)
    dates = page.labelled(r'(?:transaction\s+)?date', _date)
    references = page.labelled(r'(?:receipt|document)\s+(?:no\.?|number|reference)', _reference)
    ordinary_heading = bool(page.indices(r'RECEIPT'))
    total_label = (r'(?:total(?:\s+(?:amt|amount))?|receipt\s+total|grand\s+total|rounded\s+total)(?:\s*\(RM\))?'
                   if ordinary_heading else r'(?:total|receipt\s+total|grand\s+total|rounded\s+total)(?:\s*\(RM\))?')
    totals = page.labelled(total_label, _amount, leaders=ordinary_heading)
    cash = page.labelled(r'cash(?:\s+tendered)?', _amount, leaders=ordinary_heading)
    change = page.labelled(r'change', _amount, leaders=ordinary_heading)
    ordinary, header_dates = _ordinary_layout(page, totals)
    dates.extend(header_dates)
    kind = _classification(page, dates, references, totals, cash, ordinary)
    rounding = page.indices(r'ROUNDING\s+ADJ(?:USTMENT)?[.\u2026\s:]*') if ordinary else []
    # A labelled pre-rounding amount is not safely the final payable total.
    # Keep its original candidate and adjustment evidence; never calculate a total.
    for candidate in totals if rounding else []:
        candidate['evidence'].extend(page.evidence(i) for i in rounding)
    fields, lines, observations = {}, [], {}
    if kind['status'] == 'classified':
        merchant = page.labelled(r'(?:merchant|supplier|vendor)', lambda s: s)
        uncertain = any('_' in c['raw_text'] for c in merchant)
        if not merchant:
            # A company suffix supports a candidate, not verification that this
            # is the merchant or that imperfect OCR spelling is trustworthy.
            first_heading = min(page.boxes[i][1] for i in page.indices(r'CASH\s+BILL|RECEIPT'))
            merchant = [page.candidate(r['text'], r['text'], [i]) for i, r in enumerate(page.rows)
                        if page.boxes[i][3] < first_heading and re.search(r'\bSDN\s+BHD\b', r['text'], re.I)]
            uncertain = bool(merchant)
        currencies = []
        for i, row in enumerate(page.rows):
            s = row['text'].strip()
            match = re.fullmatch(r'(?:currency\s*:\s*)?(RM|MYR|USD|SGD|EUR|GBP)', s, re.I)
            parenthetical = re.search(r'\((RM|MYR|USD|SGD|EUR|GBP)\)', s, re.I)
            if match or parenthetical:
                code = (match or parenthetical)[1].upper()
                currencies.append(page.candidate(s, 'MYR' if code == 'RM' else code, [i]))
        fields = {'merchant': page.cell(merchant, uncertain=uncertain),
                  'transaction_date': page.cell(dates), 'receipt_number': page.cell(references),
                  'currency': page.cell(currencies), 'total_amount': page.cell(totals, uncertain=bool(rounding)),
                  'payment_method': page.cell([page.candidate(page.rows[i]['text'], 'Cash', [i])
                                              for i in page.indices(r'cash(?:\s+tendered)?[.\u2026]*' if ordinary else r'cash(?:\s+tendered)?')])}
        lines = _lines(page, totals)
        observations = {'cash_tendered': page.cell(cash), 'change': page.cell(change)}
    return {'schema_version': '0.1.0-proposed', 'mode': 'module2_public_result',
            'document_id': document_id,
            'document_type': kind, 'status': 'human_review_required', 'review_required': True,
            'accounting_approval_performed': False,
            'safety': {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False},
            'fields': fields, 'line_items': lines, 'receipt_observations': observations,
            'issues': [{'code': 'OCR_REQUIRES_REVIEW', 'detail': 'OCR spelling, item grouping and payment observations require source review; no approval or settlement inferred.'}],
            'provenance': {'adapter_version': VERSION, 'source_kind': 'rapidocr_regions',
                           'image_sha256': artifact['image_sha256'],
                           'ocr_result_canonical_json_sha256': hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                           'model_versions': deepcopy(model_versions),
                           'native_ocr_result': deepcopy(artifact)}}
