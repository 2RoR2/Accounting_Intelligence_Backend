"""Evidence-grounded heading and retail-content rules. Unknown remains reviewable."""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from uuid import uuid4
from services.extraction.layout.layout_headers import records

VERSION = '0.4.2'
TITLES = {
    'invoice': 'invoice', 'tax invoice': 'invoice', 'commercial invoice': 'invoice',
    'simplified tax invoice': 'invoice', 'taxinvoice': 'invoice',
    'bill': 'bill', 'utility bill': 'bill', 'service bill': 'bill', 'electricity bill': 'bill',
    'receipt': 'receipt', 'sales receipt': 'receipt', 'payment receipt': 'receipt', 'retail receipt': 'receipt',
    'credit note': 'credit_note', 'credit memo': 'credit_note',
    'debit note': 'debit_note', 'debit memo': 'debit_note',
    'sales invoice': 'invoice', 'invois': 'invoice', 'invois cukai': 'invoice',
    'fatura': 'invoice', 'factura': 'invoice', 'fatura simplificada': 'invoice',
    'resit': 'receipt', 'resit rasmi': 'receipt', 'struk': 'receipt',
    'recibo': 'receipt', 'bil': 'bill', 'bil elektrik': 'bill',
    'bil air': 'bill', 'consulta': 'bill',
    'nota kredit': 'credit_note', 'nota debit': 'debit_note',
}
UNSUPPORTED = {'quotation', 'purchase order',
               'pro forma invoice', 'proforma invoice', 'pro-forma invoice', 'statement of account'}

def normalize_title(text):
    text = ' '.join(text.casefold().split())
    # The existing synthetic invoice uses this exact decorative suffix.
    text = re.sub(r'\s*[-–—]\s*synthetic test data$', '', text)
    return text.strip()

# Flattened blocks need both a heading cue and document-specific field labels.
# Do not search arbitrary prose or interpret "Invoice number" / "Bill to" as titles.
MERGED_FIELDS = {
    'credit_note': ({'credit note number', 'reference'}, {'date', 'currency', 'total'}),
    'debit_note': ({'debit note number', 'reference'}, {'date', 'currency', 'total'}),
    'invoice': ({'invoice number', 'invoice no'},
                {'invoice date', 'date', 'currency', 'subtotal', 'total', 'total amount'}),
    'bill': ({'account number', 'bill reference', 'bill number'},
             {'bill date', 'due date', 'currency', 'amount due'}),
    'receipt': ({'receipt number'},
                {'transaction date', 'date', 'currency', 'total', 'total amount'}),
}
_TITLE_PATTERN = '|'.join(re.escape(t).replace(r'\ ', r'\s+')
                          for t in sorted(set(TITLES) | UNSUPPORTED, key=len, reverse=True))
_HEADING_PREFIX = re.compile(r'^\s*(?P<first>' + _TITLE_PATTERN +
    r')(?:\s*/\s*(?P<second>' + _TITLE_PATTERN + r'))?(?=$|\s)', re.IGNORECASE)
_FIELD_PATTERN = re.compile(r'(?<!\S)(' + '|'.join(re.escape(t).replace(r'\ ', r'\s+')
    for t in sorted(set().union(*(a | b for a, b in MERGED_FIELDS.values())),
                    key=len, reverse=True)) + r')\s*:\s*\S', re.IGNORECASE)


def heading_titles(raw, *, leading_block, label):
    title = normalize_title(raw)
    if title in TITLES or title in UNSUPPORTED:
        return [title]
    match = _HEADING_PREFIX.match(raw)
    if not match:
        return []
    headings = [normalize_title(t) for t in match.groups() if t is not None]
    tail = raw[match.end():]
    # A second standalone heading on the next physical line must not be
    # swallowed by a merged block and turned into a single-type document.
    if tail.lstrip(' \t').startswith(('\n', '\r')):
        for line in tail.lstrip(' \t\r\n').splitlines():
            next_title = normalize_title(line)
            if next_title not in TITLES and next_title not in UNSUPPORTED:
                break
            headings.append(next_title)
    if not tail.strip():  # Explicit slash-separated headings remain ambiguous.
        return headings
    if not leading_block and label not in {'title', 'section_header'}:
        return []
    # A leading unsupported title vetoes a supported guess even in title case.
    if any(t in UNSUPPORTED for t in headings):
        return headings
    # Uppercase prefix, an actual line break, or a Docling heading label is
    # stronger evidence than a title-case word starting an ordinary sentence.
    if not (all(t == t.upper() for t in match.groups() if t is not None)
            or tail.lstrip(' \t').startswith(('\n', '\r'))
            or label in {'title', 'section_header'}):
        return []
    if re.match(r'\s*(?:number|no\.?|date|to|reference|copy|details)\b|\s*/', tail, re.IGNORECASE) and not (
            all(TITLES.get(t) in {'credit_note', 'debit_note'} for t in headings)
            and re.match(r'\s*reference\s*:', tail, re.IGNORECASE)):
        return []
    fields = {' '.join(m[1].casefold().split()) for m in _FIELD_PATTERN.finditer(tail)}
    if any(fields & MERGED_FIELDS[TITLES[t]][0] and fields & MERGED_FIELDS[TITLES[t]][1]
           for t in headings):
        return headings
    return []


def classification_records(document):
    """Keep retained lines and body blocks; do not change field-recovery records."""
    result = list(records(document))
    seen = {item.get('self_ref') for item in result if item.get('self_ref')}
    if document.get('m2_text_lines'):
        for index, item in enumerate(document.get('texts', [])):
            ref = item.get('self_ref', f'#/texts/{index}')
            if ref not in seen and item.get('content_layer', 'body') == 'body':
                result.append({**item, 'self_ref': ref})
                seen.add(ref)
    return result


def source_evidence(item):
    return {'text': item.get('text', ''), 'source_ref': item.get('self_ref'),
            'prov': deepcopy(item.get('prov', []))}


def add_heading(candidates, candidate):
    """Corroborate an identical heading across streams without counting it twice."""
    evidence = candidate['evidence']
    ref = evidence.get('source_ref') or ''
    pages = {p.get('page_no') for p in evidence['prov']}
    for existing in candidates:
        old = existing['evidence']
        old_ref = old.get('source_ref') or ''
        cross_stream = (ref.startswith('#/texts/') and old_ref.startswith('#/m2_text_lines/')) or (
            old_ref.startswith('#/texts/') and ref.startswith('#/m2_text_lines/'))
        if (existing['matched_title'] == candidate['matched_title']
                and existing.get('value') == candidate.get('value')
                and normalize_title(old['text']) == normalize_title(evidence['text'])
                and (ref == old_ref or cross_stream and pages and pages == {
                    p.get('page_no') for p in old['prov']})):
            if ref != old_ref:
                old.setdefault('corroborating_sources', []).append(evidence)
            return
    candidates.append(candidate)


def invoice_identity_evidence(items):
    """A reference alone is insufficient: require a same-page invoice structure.

    Some invoices have no standalone title, only a leading invoice identifier.
    Use source text, never document IDs, filenames, catalog families or answers.
    This classifies the document only; it does not resolve dates or amounts.
    """
    by_page = {}
    for item in items:
        if (item.get('content_layer', 'body') != 'body'
                or item.get('label', 'text') not in {'title', 'section_header', 'text'}):
            continue
        pages = {p.get('page_no') for p in item.get('prov', [])}
        if len(pages) == 1 and None not in pages:
            by_page.setdefault(next(iter(pages)), []).append(item)
    identifier = re.compile(r'^invoice\s*(?:number|no\.?|#)\s*:?\s*'
                            r'(?=[a-z0-9./_-]*\d)[a-z0-9][a-z0-9./_-]*$', re.I)
    for page_items in by_page.values():
        # Preserve independent stream ordering when deciding whether an ID leads a page.
        streams = {}
        for item in page_items:
            stream = 'structured' if (item.get('self_ref') or '').startswith('#/texts/') else 'retained'
            streams.setdefault(stream, []).append(item)
        anchors = [item for stream in streams.values() for item in stream[:4]
                   if identifier.fullmatch(item.get('text', '').strip())]
        if not anchors:
            continue
        cues = {}
        patterns = {
            'issue_date': r'^\s*(?:date of issue|issue date|invoice date)\s*(?::.*)?$',
            'date_value': r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4})\b',
            'seller': r'^\s*(?:seller|supplier|vendor)\s*(?::.*)?$',
            'buyer': r'^\s*(?:buyer|customer|client|bill to)\s*(?::.*)?$',
            'description': r'\bdescription\b',
            'quantity': r'\b(?:qty|quantity)\b',
            'price': r'\b(?:unit price|net price|price/unit)\b',
            'total': r'\b(?:total|gross worth|net worth)\b',
        }
        for name, pattern in patterns.items():
            matches = [item for item in page_items if re.search(pattern, item.get('text', ''), re.I)]
            if matches:
                cues[name] = matches[0]
        # Requiring every cue keeps quoted invoice references and generic lists unknown.
        if set(cues) == set(patterns):
            anchor = next((a for a in anchors if a.get('label') in {'title', 'section_header'}), anchors[0])
            evidence = source_evidence(anchor)
            evidence['corroborating_sources'] = [source_evidence(item) for item in cues.values()]
            return evidence
    return None


def retail_item_evidence(document):
    """Require labelled total/tender amounts and repeated geometric item rows.

    This proposes a receipt kind only. It does not infer currency, identity,
    dates or settlement, and never overrides a heading or unsupported veto.
    """
    items=[]
    for item in records(document):
        prov=item.get('prov',[])
        if len(prov)!=1 or item.get('content_layer','body')!='body':continue
        p=prov[0];b=p.get('bbox') or {}
        if b.get('coord_origin')!='TOPLEFT' or not all(k in b for k in ('l','t','r','b')):continue
        if b['b']<=b['t'] or b['r']<=b['l']:continue
        items.append((item,p['page_no'],b))
    def aligned(a,b):
        return abs((a['t']+a['b']-b['t']-b['b'])/2)<=min(a['b']-a['t'],b['b']-b['t'])*.65
    numeric=re.compile(r'^(?:RM\s*)?\d+(?:[.,]\d+)*$',re.I)
    for total,page,box in items:
        if not re.fullmatch(r'(?:grand\s+)?total',total.get('text','').strip(),re.I):continue
        total_values=[(x,p,b) for x,p,b in items if p==page and b['l']>=box['r'] and aligned(box,b) and numeric.fullmatch(x.get('text','').strip())]
        if len(total_values)!=1:continue
        tenders=[(x,p,b) for x,p,b in items if p==page and b['t']>=box['b']-5 and re.fullmatch(r'cash|tunai',x.get('text','').strip(),re.I)]
        for tender,_,tb in tenders:
            amounts=[x for x,p,b in items if p==page and b['l']>=tb['r'] and aligned(tb,b) and numeric.fullmatch(x.get('text','').strip())]
            if len(amounts)!=1:continue
            pairs=[]
            for text,p,b in items:
                if p!=page or b['t']>=box['t'] or not re.search(r'[A-Za-z]',text.get('text','')):continue
                if re.fullmatch(r'.*(?:total|cash|change|balance|tax|date|invoice|bill|receipt).*',text.get('text',''),re.I):continue
                values=[x for x,q,c in items if q==page and c['l']>=max(b['r'],total_values[0][2]['l']-30) and aligned(b,c) and numeric.fullmatch(x.get('text','').strip())]
                if len(values)==1:pairs.extend([text,values[0]])
            if len(pairs)>=4:return [total,total_values[0][0],tender,amounts[0],*pairs]
    # A title-free POS bill may omit a tender row. Require cashier, dated
    # transaction identity, a final labelled total, and repeated aligned items.
    # This proposes document kind only, never payment/settlement status.
    for total,page,box in items:
        if not re.fullmatch(r'(?:grand|net)\s+total\s*:?',total.get('text','').strip(),re.I):continue
        totals=[x for x,p,b in items if p==page and b['l']>=box['r'] and aligned(box,b) and numeric.fullmatch(x.get('text','').strip())]
        cashiers=[x for x,p,b in items if p==page and b['b']<box['t'] and re.fullmatch(r'cashier\s*(?::.*)?',x.get('text','').strip(),re.I)]
        refs=[x for x,p,b in items if p==page and b['b']<box['t'] and re.match(r'^(?:receipt|bill|transaction)\s*(?:no\.?|number|#)\b',x.get('text','').strip(),re.I)]
        dates=[x for x,p,b in items if p==page and b['b']<box['t'] and re.search(r'\b\d{1,2}[/.-]\d{1,2}[/.-]\d{4}\s+\d{1,2}:\d{2}',x.get('text',''))]
        if len(totals)!=1 or not cashiers or not refs or not dates:continue
        pairs=[]
        for x,p,b in items:
            if p!=page or not dates[0].get('prov') or b['t']<=dates[0]['prov'][0]['bbox']['b'] or b['b']>=box['t']:continue
            if not re.match(r'^\d+\s+[A-Za-z]',x.get('text','')):continue
            values=[y for y,q,c in items if q==page and c['l']>=b['r'] and aligned(b,c)
                    and re.fullmatch(r'\d+\.\d{2}(?:\s+[A-Z]{1,3})?',y.get('text','').strip())]
            if len(values)==1:pairs.extend([x,values[0]])
        if len(pairs)>=4:return [total,totals[0],cashiers[0],refs[0],dates[0],*pairs]
    return []


def context_pages(document):
    """Body records with credible geometry, grouped by their actual source page."""
    pages = {}
    for item in records(document):
        prov = item.get('prov', [])
        if len(prov) != 1 or item.get('content_layer', 'body') != 'body':
            continue
        page = prov[0].get('page_no')
        size = document.get('pages', {}).get(str(page), {}).get('size', {})
        box = prov[0].get('bbox') or {}
        try:
            left, top, right, bottom = (float(box[k]) for k in ('l', 't', 'r', 'b'))
            width, height = float(size['width']), float(size['height'])
            if box.get('coord_origin') == 'BOTTOMLEFT':
                top, bottom = height - top, height - bottom
            elif box.get('coord_origin') != 'TOPLEFT':
                continue
            if not all(math.isfinite(n) for n in (left, top, right, bottom, width, height)):
                continue
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        pages.setdefault(page, []).append((item, (left, top, right, bottom)))
    return pages


CONTEXT_MONEY = r'(?:[A-Z]{3}|RM|Rp|[$€£])?\s*\d+(?:[,.]\d{3})*[.,]\d{2}(?:\s*[A-Z]{3})?'


def linked_context(positioned, pattern, valid):
    """A labelled inline value or exactly one nearby value on the same page."""
    pattern = '(?:' + pattern + ')'
    for label, a in positioned:
        text = label.get('text', '').strip()
        inline = re.fullmatch(pattern + r'\s*:\s*(\S.*?)\s*', text, re.I)
        if inline and valid(inline[1]):
            return [label]
        if not re.fullmatch(pattern + r'\s*[:#]?', text, re.I):
            continue
        neighbours = []
        h = a[3] - a[1]
        for value, box in positioned:
            if value is label or not valid(value.get('text', '').strip()):
                continue
            aligned = abs((a[1] + a[3] - box[1] - box[3]) / 2) <= min(h, box[3] - box[1]) * .65
            right = aligned and 0 <= box[0] - a[2] <= h * 12
            below = (0 <= box[1] - a[3] <= h * 1.8
                     and abs(box[0] - a[0]) <= h * 1.5)
            if right or below:
                neighbours.append(value)
        if len(neighbours) == 1:
            return [label, neighbours[0]]
    return []


def retail_transaction_context(document):
    """POS itemization and dated total/tender/change evidence, never settlement.

    A damaged monetary read stays unresolved; integers are not repaired into
    decimal amounts. All cues must belong to the same source page.
    """
    partial = []
    tender_pattern = r'cash\s*(?:tendered)?|tunai|paid\s+amount'
    for positioned in context_pages(document).values():
        headers = [x for x, _ in positioned if re.fullmatch(r'qty(?:\s+item)?|quantity|item', x.get('text', '').strip(), re.I)]
        dates = [x for x, _ in positioned if re.search(r'\b\d{1,2}[/.-]\d{1,2}[/.-]\d{4}\b', x.get('text', ''))]
        if not headers or not dates:
            continue
        monetary = lambda value: bool(re.fullmatch(CONTEXT_MONEY, value))
        # A TOTAL column beside QTY/ITEM labels prices individual goods.
        # It cannot supply the document-total cue used with tender/change.
        header_boxes = [b for x, b in positioned if x in headers]
        summary = [(x, b) for x, b in positioned if not (
            re.fullmatch(r'total\s*:?', x.get('text', '').strip(), re.I)
            and any(abs((b[1] + b[3] - h[1] - h[3]) / 2) <= min(b[3] - b[1], h[3] - h[1]) * .65
                    for h in header_boxes))]
        total = linked_context(summary, r'total(?:\s+(?:rounded|amt\s+payable))?', monetary)
        tender = linked_context(positioned, tender_pattern, monetary)
        change = linked_context(positioned, r'change|baki|kembalian', monetary)
        if total and tender and change:
            return [headers[0], dates[0], *total, *tender, *change], True
        tender_labels = [x for x, _ in positioned if re.fullmatch(
            r'(?:' + tender_pattern + r'|visa\s+card|master\s*card)\s*:?', x.get('text', '').strip(), re.I)]
        if total and tender_labels:
            partial = [headers[0], dates[0], *total, *tender_labels, *change]
    return partial, False


def retail_pos_identity_context(document):
    """POS identity plus a priced item table and final total; no payment cues.

    Every cue must have credible same-page geometry. Operator, reference and
    timestamp must precede the table. This proposes business function only.
    """
    from services.extraction.layout.layout_rows import DESCRIPTION, PRICE, AMOUNT, SUMMARY

    def operator(value):
        value = value.lstrip(': ').strip()
        return (bool(re.fullmatch(r'(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9 ._\x27-]{0,79}', value))
                and not re.match(r'cashier|operator|date|time|invoice|receipt|bill\b|customer|buyer|address', value, re.I))

    def reference(value):
        value = value.lstrip(': ').strip()
        return (bool(re.fullmatch(r'(?=.*\d)[A-Za-z0-9][A-Za-z0-9./_-]{0,99}', value))
                and not re.fullmatch(CONTEXT_MONEY, value))

    def timestamp(value):
        match = re.fullmatch(r'(?:(?:transaction\s+)?date(?:/time)?\s*)?:?\s*'
            r'(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4})\s+'
            r'(\d{1,2}:\d{2}(?::\d{2})?)(?:\s*(AM|PM))?', value.strip(), re.I)
        if not match:
            return False
        date_text = re.sub(r'[.-]', '/', match[1])
        suffix = ' %p' if match[3] else ''
        time_format = ('%I' if match[3] else '%H') + ':%M' + (':%S' if match[2].count(':') == 2 else '')
        for fmt in ('%Y/%m/%d', '%d/%m/%Y', '%m/%d/%Y'):
            try:
                datetime.strptime(date_text + ' ' + match[2] + (' ' + match[3].upper() if match[3] else ''),
                                  fmt + ' ' + time_format + suffix)
                return True
            except ValueError:
                pass
        return False

    def aligned(a, b):
        return abs((a[1] + a[3] - b[1] - b[3]) / 2) <= min(a[3] - a[1], b[3] - b[1]) * .65

    partial = []
    final_pattern = r'(?:grand|net)\s+total|total(?:\s+(?:amount|rounded|amt\s+payable))?'
    for positioned in context_pages(document).values():
        for description, db in positioned:
            if not DESCRIPTION.fullmatch(description.get('text', '').strip()):
                continue
            prices = [(x, b) for x, b in positioned if PRICE.fullmatch(x.get('text', '').strip())
                      and b[0] >= db[2] and aligned(db, b)]
            amounts = [(x, b) for x, b in positioned if AMOUNT.fullmatch(x.get('text', '').strip())
                       and b[0] >= db[2] and aligned(db, b)]
            if len(prices) != 1 or len(amounts) != 1 or prices[0][1][2] > amounts[0][1][0]:
                continue
            price, pb = prices[0]; amount, ab = amounts[0]
            header_top = min(db[1], pb[1], ab[1]); header_bottom = max(db[3], pb[3], ab[3])
            header = [(x, b) for x, b in positioned if b[3] <= header_top]
            clerk = linked_context(header, r'cashier|operator|till\s+operator|served\s+by', operator)
            identity = linked_context(header, r'(?:receipt|invoice|bill|transaction)\s*(?:no\.?|number|#)', reference)
            dated = [x for x, _ in header if timestamp(x.get('text', ''))]
            if not clerk or not identity or len(dated) != 1:
                continue
            partial = [*identity, dated[0], *clerk, description, price, amount]
            # Final totals follow the item table and precede tax-summary rows.
            tax_top = min([b[1] for x, b in positioned if re.fullmatch(r'(?:gst|tax)\s+summary',
                           x.get('text', '').strip(), re.I)] or [float('inf')])
            summary = [(x, b) for x, b in positioned if header_bottom < b[1] < tax_top]
            totals = []
            for label, box in summary:
                if not re.fullmatch(final_pattern + r'\s*:?', label.get('text', '').strip(), re.I):
                    continue
                neighbours = [(label, box)] + [(x, b) for x, b in summary if x is not label
                    and not re.fullmatch(final_pattern + r'\s*:?', x.get('text', '').strip(), re.I)]
                linked = linked_context(neighbours, final_pattern, lambda v: bool(re.fullmatch(CONTEXT_MONEY, v)))
                if linked:
                    totals.append((linked, box))
            partial += [x for linked, _ in totals for x in linked]
            if not totals or len({pair[0][-1]['text'].strip() for pair in totals}) != 1:
                continue
            bottom = min(box[1] for _, box in totals)
            body = [(x, b) for x, b in positioned if header_bottom - (ab[3] - ab[1]) * .2 <= b[1] and b[3] < bottom]
            for charge, cb in body:
                if (abs(cb[2] - ab[2]) > max(ab[3] - ab[1], cb[3] - cb[1]) * 1.5
                        or not re.fullmatch(CONTEXT_MONEY + r'(?:\s+[A-Z]{1,3})?', charge.get('text', '').strip())):
                    continue
                rows = [x for x, b in body if b[2] <= pb[0] and aligned(cb, b)
                        and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ./_\x27-]{0,99}', x.get('text', '').strip())
                        and not SUMMARY.fullmatch(x.get('text', '').strip())]
                price_values = [x for x, b in body if aligned(cb, b)
                    and abs(b[2] - pb[2]) <= max(pb[3] - pb[1], b[3] - b[1]) * 1.5
                    and re.fullmatch(r'(?:\d+(?:[.,]\d+)?\s+)?' + CONTEXT_MONEY, x.get('text', '').strip())]
                if len(rows) == 1 and len(price_values) == 1:
                    return partial + [rows[0], price_values[0], charge], True
    return partial, False


def utility_bill_context(document):
    """Billed utility usage plus linked amount due and payment terms.

    A supplier name, account label or amount due alone is insufficient. These
    source cues classify business function without resolving accounting values.
    """
    service = re.compile(r'^(?:electricity|gas|water)\s+(?:consumption|usage|supply\b|'
        r'[-–]\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b)', re.I)

    def deadline(value):
        if re.fullmatch(r'(?:on|upon)\s+(?:receipt|demand)|net\s+\d{1,3}(?:\s+days)?|'
                        r'(?:payment\s+)?due\s+within\s+\d{1,3}\s+days', value, re.I):
            return True
        # A printed date can distinguish billing context even when its day/month
        # order remains unresolved by the separate extraction date policy.
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%m-%d-%Y'):
            try:
                datetime.strptime(value, fmt)
                return True
            except ValueError:
                pass
        return False

    partial = []
    for positioned in context_pages(document).values():
        usage = []
        for item, box in positioned:
            if not service.match(item.get('text', '').strip()):
                continue
            headers = [x for x, b in positioned if re.fullmatch(r'description|particulars', x.get('text', '').strip(), re.I)
                       and b[3] <= box[1] and abs(b[0] - box[0]) <= (box[3] - box[1]) * 2]
            charges = [(x, b) for x, b in positioned if b[0] >= box[2]
                       and abs((b[1] + b[3] - box[1] - box[3]) / 2) <= min(b[3] - b[1], box[3] - box[1]) * .65
                       and re.fullmatch(CONTEXT_MONEY, x.get('text', '').strip())]
            if headers and charges:
                usage = [headers[0], item, max(charges, key=lambda pair: pair[1][2])[0]]
                break
        if not usage:
            continue
        due = linked_context(positioned, r'(?:total\s+)?amount\s+due', lambda v: bool(re.fullmatch(CONTEXT_MONEY, v)))
        terms = linked_context(positioned, r'due\s+date|payment\s+deadline|payment\s+terms', deadline)
        if not terms:
            terms = [x for x, _ in positioned if re.fullmatch(
                r'(?:notes?\s*:\s*)?(?:payment\s+)?due\s+within\s+\d{1,3}\s+days', x.get('text', '').strip(), re.I)][:1]
        if due and terms:
            return usage + due + terms, True
        if due or terms:
            partial = usage + due + terms
    return partial, False


def classify_document(document):
    if document.get('schema_name') != 'DoclingDocument':
        raise ValueError('Expected a DoclingDocument export')
    candidates, excluded = [], []
    leading_block = True
    items = classification_records(document)
    for index, item in enumerate(items):
        if item.get('content_layer', 'body') != 'body':
            continue
        # Text allows Docling's unlabelled-heading cases, but requires a whole
        # text item to match. We never match an invoice number or 'Bill to'.
        if item.get('label', 'text') not in {'title', 'section_header', 'text'}:
            continue
        raw = item.get('text', '')
        title = normalize_title(raw)
        decorated = re.fullmatch(r'[*#\s]*(receipt|resit|tax invoice|sales invoice)[*#\s]*(?:<[^>]+>)?', title)
        fiscal = re.fullmatch(r'(?:original\s+|duplicado\s+)?(fatura simplificada|fatura|factura)\s+(?:nr[.:º]*|n[.º]*|no[.:]*)\s*[:：]?\s*.+', title)
        if decorated:
            title = decorated[1]
        elif fiscal:
            title = fiscal[1]
        elif re.fullmatch(r'#[0-9]+\s+.{1,80}\s+sales invoice', title):
            title = 'sales invoice'
        # OCR often keeps a fiscal heading and its identifier in one block.
        # Match specific headings, never a bare 'invoice number' / 'bill to'.
        for heading in sorted(TITLES, key=len, reverse=True):
            if re.fullmatch(re.escape(heading) + r'\s*(?:[#:：]\s*|\s+)(?=[a-z0-9/-]*\d)[a-z0-9/-]+', title):
                title = heading
                break
        if title in {'bill / invoice', 'invoice / bill', 'bill/invoice', 'invoice/bill'}:
            evidence = {'text': raw, 'source_ref': item.get('self_ref', f'#/texts/{index}'),
                        'prov': deepcopy(item.get('prov', []))}
            for kind in ('bill', 'invoice'):
                add_heading(candidates, {'value': kind, 'matched_title': title, 'evidence': deepcopy(evidence)})
            continue
        titles = ([title] if title in TITLES or title in UNSUPPORTED else
                  heading_titles(raw, leading_block=leading_block, label=item.get('label', 'text')))
        if (not titles and item.get('label') in {'title','section_header'}
                and re.fullmatch(r'(?:\S+\s+){1,3}invoice', title)):
            # A damaged/unknown invoice qualifier is reviewable; do not let
            # retail payment cues silently override this heading fragment.
            add_heading(excluded, {'matched_title': title, 'evidence': source_evidence(item)})
        if raw.strip():
            leading_block = False
        for title in titles:
            evidence = {'text': raw, 'source_ref': item.get('self_ref', f'#/texts/{index}'),
                        'prov': deepcopy(item.get('prov', []))}
            if title in UNSUPPORTED:
                add_heading(excluded, {'matched_title': title, 'evidence': evidence})
            else:
                add_heading(candidates, {'value': TITLES[title], 'matched_title': title, 'evidence': evidence})
    method = 'explicit_heading_rules'
    content_reason = 'RETAIL_PAYMENT_EVIDENCE'
    utility, complete_utility = utility_bill_context(document) if not excluded else ([], False)
    retail_context, complete_retail = retail_transaction_context(document) if not excluded else ([], False)
    incomplete_utility = bool(utility and not complete_utility)
    incomplete_retail = False
    fiscal_titles = {'tax invoice', 'taxinvoice', 'simplified tax invoice', 'invois cukai'}

    def compatible_fiscal_titles(evidence):
        pages = {p.get('page_no') for p in evidence.get('prov', [])}
        return all(c['value'] == 'invoice' and c['matched_title'] in fiscal_titles
                   and pages == {p.get('page_no') for p in c['evidence'].get('prov', [])}
                   for c in candidates)

    if utility:
        method = 'content_evidence_rules'
        content_reason = 'UTILITY_USAGE_AND_BILLING_EVIDENCE'
        evidence = source_evidence(utility[0])
        evidence['corroborating_sources'] = [source_evidence(item) for item in utility[1:]]
        if complete_utility and compatible_fiscal_titles(evidence):
            # A fiscal title is compatible with a utility bill's business function.
            # Keep every printed heading, including cross-stream corroboration.
            evidence['printed_headings'] = deepcopy(candidates)
            candidates = []
        candidates.append({'value': 'bill', 'matched_title': 'utility usage + amount due + payment deadline',
                           'evidence': evidence})
        if retail := retail_item_evidence(document):
            retail_evidence = source_evidence(retail[0])
            retail_evidence['corroborating_sources'] = [source_evidence(item) for item in retail[1:]]
            candidates.append({'value': 'receipt', 'matched_title': 'competing retail transaction evidence',
                               'evidence': retail_evidence})
    if retail_context and (complete_retail or any(c['value'] == 'invoice' for c in candidates)):
        method = 'content_evidence_rules'
        content_reason = 'RETAIL_TOTAL_TENDER_CHANGE_EVIDENCE'
        incomplete_retail = not complete_retail
        evidence = source_evidence(retail_context[0])
        evidence['corroborating_sources'] = [source_evidence(item) for item in retail_context[1:]]
        if complete_retail and compatible_fiscal_titles(evidence):
            evidence['printed_headings'] = deepcopy(candidates)
            candidates = []
        candidates.append({'value': 'receipt', 'matched_title': 'dated itemization + total + tender + change',
                           'evidence': evidence})
    if not excluded and not complete_retail and not any(c['value'] in {'credit_note', 'debit_note'} for c in candidates):
        pos, complete_pos = retail_pos_identity_context(document)
        if pos:
            method = 'content_evidence_rules'
            content_reason = 'POS_IDENTITY_ITEMIZATION_AND_TOTAL_EVIDENCE'
            evidence = source_evidence(pos[0])
            evidence['corroborating_sources'] = [source_evidence(item) for item in pos[1:]]
            invoice = invoice_identity_evidence(items)
            if invoice:
                evidence['competing_invoice_identity'] = invoice
            if complete_pos and incomplete_retail:
                # Incomplete payment reads do not veto independent POS identity.
                # Retain their evidence without treating them as settlement.
                payment_candidates = [c for c in candidates if c['value'] == 'receipt'
                                      and c['matched_title'] == 'dated itemization + total + tender + change']
                evidence['incomplete_payment_context'] = deepcopy(payment_candidates)
                candidates = [c for c in candidates if c not in payment_candidates]
            if complete_pos and compatible_fiscal_titles(evidence) and not invoice:
                evidence['printed_headings'] = deepcopy(candidates)
                candidates = []
            if invoice and not any(c['value'] == 'invoice' for c in candidates):
                candidates.append({'value': 'invoice', 'matched_title': 'competing invoice identity evidence', 'evidence': invoice})
            candidates.append({'value': 'receipt', 'matched_title': 'operator + reference + timestamp + priced items + final total',
                               'evidence': evidence})
            incomplete_retail = not complete_pos
    if not candidates and not excluded:
        # A title-free till slip needs independent payment and total signals.
        # Never infer a receipt from filename, currency or a total alone.
        texts = list(document.get('texts', []))
        for ti, table in enumerate(document.get('tables', [])):
            for ci, cell in enumerate(table.get('data', {}).get('table_cells', [])):
                texts.append({'text': cell.get('text', ''), 'self_ref': f'#/tables/{ti}/data/table_cells/{ci}',
                              'prov': deepcopy(table.get('prov', []))})
        body = [item for item in texts if item.get('content_layer', 'body') == 'body']
        total = [item for item in body if re.search(r'(?im)^\s*(?:grand\s+total|total(?:\s+amount)?|jumlah(?:\s+besar)?)\b', item.get('text', ''))]
        change = [item for item in body if re.search(r'(?im)^\s*(?:change|baki|kembalian|troco)\b', item.get('text', ''))]
        payment = [item for item in body if re.search(r'(?im)^\s*(?:cash|tunai|dinheiro|payment\s+method|paid\s+by)\b', item.get('text', ''))]
        if total and change and payment:
            method = 'content_evidence_rules'
            candidates = [{'value': 'receipt', 'matched_title': 'total + tender + change',
                           'evidence': {'text': item.get('text', ''), 'source_ref': item.get('self_ref'),
                                        'prov': deepcopy(item.get('prov', []))}}
                          for item in (total[0], payment[0], change[0])]
        elif retail := retail_item_evidence(document):
            method = 'content_evidence_rules'
            tender = any(re.fullmatch(r'cash|tunai',item.get('text','').strip(),re.I) for item in retail)
            if not tender:
                content_reason = 'RETAIL_TRANSACTION_EVIDENCE'
            candidates = [{'value':'receipt','matched_title':('item rows + total + cash tender' if tender else 'POS identity + timestamp + item rows + final total'),
                           'evidence':{'text':item.get('text',''),'source_ref':item.get('self_ref'),
                                       'prov':deepcopy(item.get('prov',[]))}} for item in retail]
        elif invoice := invoice_identity_evidence(items):
            method = 'content_evidence_rules'
            content_reason = 'INVOICE_IDENTITY_AND_FINANCIAL_EVIDENCE'
            candidates = [{'value': 'invoice', 'matched_title': 'invoice identity + issue date + parties + financial table',
                           'evidence': invoice}]
    values = sorted({c['value'] for c in candidates})
    if excluded:
        value, status, reason = 'unknown', 'unsupported', 'UNSUPPORTED_DOCUMENT_HEADING'
    elif incomplete_utility:
        value, status, reason = 'unknown', 'ambiguous', 'INCOMPLETE_UTILITY_BILL_CONTEXT'
    elif incomplete_retail:
        value, status, reason = 'unknown', 'ambiguous', 'INCOMPLETE_RETAIL_TRANSACTION_CONTEXT'
    elif len(values) > 1:
        value, status, reason = 'unknown', 'ambiguous', 'MULTIPLE_DOCUMENT_TYPES'
    elif len(values) == 1:
        value, status, reason = values[0], 'classified', ('EXPLICIT_DOCUMENT_HEADING' if method == 'explicit_heading_rules' else content_reason)
    else:
        value, status, reason = 'unknown', 'unknown', 'NO_SUPPORTED_DOCUMENT_HEADING'
    return {'classifier_version': VERSION, 'method': method,
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


