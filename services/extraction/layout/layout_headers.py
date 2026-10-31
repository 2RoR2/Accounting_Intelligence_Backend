"""Conservative OCR label/value recovery with retained source geometry."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import re
from services.extraction.contracts.accounting_contract import CURRENCY_MINOR_UNITS, decimal_text


ALIASES = {
    'invoice': {'invoice no.': 'invoice_number', 'invoice #': 'invoice_number',
                'no invois': 'invoice_number', 'nº documento': 'invoice_number',
                'data': 'invoice_date', 'tarikh': 'invoice_date', 'total a pagar': 'total_amount',
                'valor total': 'total_amount', 'jumlah': 'total_amount', 'total iva': 'tax_amount',
                'doc no': 'invoice_number', 'doc no.': 'invoice_number', 'document no.': 'invoice_number',
                'billing address': 'buyer_name', 'bill to': 'buyer_name',
                'sub total': 'subtotal', 'total payable': 'total_amount', 'tax amt': 'tax_amount',
                'total amount due': 'amount_due'},
    'receipt': {'receipt no': 'receipt_number', 'receipt no.': 'receipt_number',
                'no resit': 'receipt_number', 'no. struk': 'receipt_number',
                'tarikh': 'transaction_date', 'tanggal': 'transaction_date',
                'grand total': 'total_amount', 'jumlah': 'total_amount',
                'sub total': 'subtotal', 'sst': 'tax_amount', 'pajak': 'tax_amount', 'tax inclusive': 'tax_amount',
                'doc no': 'receipt_number', 'doc no.': 'receipt_number', 'total sales': 'total_amount'},
    'bill': {'bill no': 'bill_reference', 'no bil': 'bill_reference',
             'no akaun': 'account_number', 'tarikh bil': 'bill_date',
             'tarikh akhir': 'due_date', 'jumlah perlu dibayar': 'amount_due',
             'total a pagar': 'amount_due', 'valor total': 'total_amount', 'total': 'total_amount',
             'data': 'bill_date', 'total iva': 'tax_amount'},
}


def records(document):
    if document.get('m2_text_lines'):
        return document['m2_text_lines']
    result = []
    for i, item in enumerate(document.get('texts', [])):
        if item.get('content_layer', 'body') == 'body':
            result.append({**item, 'self_ref': item.get('self_ref', f'#/texts/{i}')})
    for ti, table in enumerate(document.get('tables', [])):
        for ci, cell in enumerate(table.get('data', {}).get('table_cells', [])):
            result.append({'text': cell.get('text', ''),
                           'self_ref': f'#/tables/{ti}/data/table_cells/{ci}',
                           'prov': [{**p, 'bbox': cell.get('bbox') or p.get('bbox')} for p in table.get('prov', [])]})
    return result


def normalized_date(raw):
    value = raw.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value).isoformat()
    match = re.fullmatch(r'(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?:\s+\d{2}:\d{2}(?::\d{2})?)?', value)
    if not match:
        raise ValueError('Unrecognised date')
    first, second, year = map(int, match.groups())
    if first <= 12 and second <= 12 and first != second:
        raise ValueError('Ambiguous day/month order')
    day, month = (first, second) if first > 12 or first == second else (second, first)
    return date(year, month, day).isoformat()


SPACING_POLICY = 'party-line-two-read-whitespace-insertions/1'


def verified_spacing(original, readings):
    """Accept two agreeing OCR reads that only insert word separators.

    Preserve every original non-whitespace character and every existing word
    boundary. Never repair spelling, case, punctuation, identifiers or numbers.
    """
    if len(readings) != 2 or not all(isinstance(x, str) for x in readings):
        return None
    candidate = ' '.join(readings[0].split())
    if candidate != ' '.join(readings[1].split()) or not candidate:
        return None
    if re.sub(r'\s+', '', original) != re.sub(r'\s+', '', candidate):
        return None
    def gaps(value):
        parts = value.split(); count = 0; result = set()
        for part in parts[:-1]:
            count += len(part); result.add(count)
        return result
    if not gaps(original) <= gaps(candidate):
        return None
    return candidate if gaps(candidate) > gaps(original) else None


def normalized_amount(raw, currency):
    text = raw.strip()
    text = re.sub(r'^(?:RM|MYR|Rp\.?|IDR|EUR|€)\s*|\s*(?:MYR|IDR|EUR|Euros?|€)$', '', text, flags=re.I).strip()
    if currency == 'EUR' and re.fullmatch(r'-?(?:\d+|\d{1,3}(?:[. ]\d{3})+),\d{2}', text):
        text = text.replace('.', '').replace(' ', '').replace(',', '.')
    elif currency == 'IDR' and re.fullmatch(r'-?\d{1,3}(?:\.\d{3})+(?:,\d{2})?', text):
        text = text.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?', text):
        text = text.replace(',', '')
    if not re.fullmatch(r'-?\d+(?:\.\d{1,2})?', text):
        raise ValueError('Ambiguous amount')
    return text


def recover_headers(document, kind, labels, found, location):
    if kind == 'unknown':
        return
    items = records(document)
    labels = {**labels, **ALIASES.get(kind, {})}
    money_keys = {'subtotal', 'tax_amount', 'total_amount', 'amount_due', 'discount', 'document_charges', 'amount_paid',
                  'total_excluding_tax', 'total_including_tax'}
    party_keys = {'supplier_name', 'buyer_name'} if kind == 'invoice' else set()
    party_details = {'supplier_address', 'customer_address', 'supplier_tax_id', 'customer_tax_id'}
    party_roles = r'(?:seller|supplier|vendor|client|customer|buyer|bill\s+to|billing\s+address)'
    has_party_role = any(re.search(r'(?<!\w)' + party_roles + r'\s*(?::|$)', item.get('text', ''), re.I)
                         for item in items)
    label_names={label.casefold().rstrip(':. ') for label in labels} | {'page no','sales executive','cashier','name','ref','customer account'}

    def plausible(key, value):
        if value.strip().casefold().rstrip(':. ') in label_names:
            return False
        if key in party_keys and (not re.search(r'[A-Za-z]', value)
                or re.search(r'(?im)^\s*' + party_roles + r'\s*[:：]?\s*$', value)
                or re.search(r'(?im)^\s*(?:\d|tel\b|fax\b|phone\b|tax\s*id\b|gstin\b|iban\b|address\b)', value)):
            return False
        if key in {'invoice_date','bill_date','transaction_date','due_date'}:
            return bool(re.fullmatch(r'(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4})(?:\s+\d{2}:\d{2}(?::\d{2})?)?',value.strip()))
        if key == 'transaction_time':
            return bool(re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?',value.strip()))
        if key == 'buyer_name' and (re.fullmatch(r'(?:account|account number|account no\.?|delivery address|billing address)', value.strip(), re.I)
                                   or re.match(r'^\d|^(?:tel|fax|phone|attn)\b', value.strip(), re.I)):
            return False
        if key in {'invoice_number', 'receipt_number'} and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./_-]{0,99}', value.strip()):
            return False
        if key in {'invoice_number', 'receipt_number'} and value.strip().casefold().rstrip(':. ') in {label.rstrip(':. ') for label in labels} | {'page no', 'sales executive', 'cashier', 'name'}:
            return False
        if key in money_keys:
            try:
                # Locale conversion happens after explicit currency recovery.
                # This rejects words, bare currency labels and percentages as
                # amount neighbours without treating them as conflicting totals.
                normalized_amount(value, None)
            except ValueError:
                return bool(re.fullmatch(r'(?:-?\d+[., ]?)+\s*(?:EUR|Euros?|€)?', value.strip(), re.I))
        return True

    def evidence(item):
        return [{**location(document, p.get('page_no'), p.get('bbox'), item['self_ref'], item.get('text', '')),
                 'from_ocr': item.get('from_ocr'), 'ocr_score': item.get('ocr_score'),
                 **({'ocr_spacing': deepcopy(item['ocr_spacing'])} if item.get('ocr_spacing') else {})}
                for p in item.get('prov', [])]

    def party_text(item):
        original = item.get('text', '').strip()
        retry = item.get('ocr_spacing') or {}
        if retry.get('policy_version') == SPACING_POLICY and item.get('from_ocr') is True:
            value = verified_spacing(original, retry.get('readings', []))
            if value and value == retry.get('value'):
                return value
        return original

    for key in party_keys:
        for candidate in found.get(key, []):
            if candidate['value'] and not plausible(key, candidate['value']):
                candidate['value'] = ''  # Retain rejected source evidence for review.

    def party_position(item):
        entries = evidence(item)
        if len(entries) != 1 or not entries[0].get('bbox'):
            return None
        box = entries[0]['bbox']
        return entries[0] if box['r'] > box['l'] and box['b'] > box['t'] else None

    def tax_identifier(value):
        return (bool(re.fullmatch(r'(?=.*\d)[A-Za-z0-9][A-Za-z0-9./-]{1,63}', value))
                and not re.fullmatch(r'\d+[.,]\d{2}', value))

    # The four optional text fields still need source geometry. An explicit
    # party-qualified label can stand alone, but an unlabelled number cannot.
    for key in party_details & set(found):
        for candidate in found[key]:
            ev = candidate['evidence']
            if (not ev or any(not e.get('bbox') or e['bbox']['r'] <= e['bbox']['l']
                              or e['bbox']['b'] <= e['bbox']['t'] for e in ev)
                    or (key.endswith('_tax_id') and candidate['value']
                        and not tax_identifier(candidate['value']))):
                candidate['value'] = ''

    def add(key, raw, value, sources):
        if key in party_keys:
            replacements = {item.get('text', '').strip(): party_text(item) for item in sources}
            value = '\n'.join(replacements.get(line, line) for line in value.split('\n'))
        ev = [entry for item in sources for entry in evidence(item)]
        for entry in ev:
            entry['bbox_scope'] = 'source_block'
        refs = {item['self_ref'] for item in sources}
        found[key][:] = [c for c in found[key] if c['value'] != '' or not any(e.get('source_ref') in refs for e in c.get('evidence', []))]
        found[key].append({'raw_text': raw, 'value': value, 'evidence': ev})

    def tax_header(item):
        text = item.get('text','').strip().casefold()
        if text not in {'total','iva','tax','base','liquido','líquido'}:
            return False
        here = evidence(item)
        if len(here)!=1 or not here[0].get('bbox'):
            return False
        a=here[0]['bbox']; h=a['b']-a['t']
        peers=[]
        for other in items:
            if other is item or not re.fullmatch(r'(?:taxa|iva%|tax rate|base|liquido|líquido|iva|tax)',other.get('text','').strip(),re.I):
                continue
            there=evidence(other)
            if len(there)==1 and there[0].get('bbox') and there[0]['page']==here[0]['page']:
                b=there[0]['bbox']
                if abs((a['t']+a['b']-b['t']-b['b'])/2)<h*.7:
                    peers.append(other)
        return len(peers)>=2

    def item_header(item):
        here = evidence(item)
        if len(here) != 1 or not here[0].get('bbox'):
            return False
        a = here[0]['bbox']; h = a['b'] - a['t']
        for other in items:
            if not re.fullmatch(r'description|item|qty|quantity|(?:qty|quantity)\s+(?:item|description)|price/unit|unit price|u/p', other.get('text', '').strip(), re.I):
                continue
            there = evidence(other)
            if len(there) == 1 and there[0].get('bbox') and there[0]['page'] == here[0]['page']:
                b = there[0]['bbox']
                if abs((a['t']+a['b']-b['t']-b['b'])/2) < h:
                    return True
        return False

    summary_refs = set()
    if kind == 'invoice':
        # Inspect both streams independently: a retained line can merge two cells.
        # Table cells retain their own original boxes and exact source references.
        structured = [{**x, 'self_ref': x.get('self_ref', f'#/texts/{i}')}
                      for i, x in enumerate(document.get('texts', []))
                      if x.get('content_layer', 'body') == 'body']
        for ti, table in enumerate(document.get('tables', [])):
            prov = table.get('prov', [])
            if len(prov) != 1 or table.get('data', {}).get('orientation', 'rot_0') != 'rot_0':
                continue
            for ci, cell in enumerate(table.get('data', {}).get('table_cells', [])):
                if cell.get('row_span', 1) != 1 or cell.get('col_span', 1) != 1:
                    continue
                structured.append({'text': cell.get('text', ''),
                    'self_ref': f'#/tables/{ti}/data/table_cells/{ci}',
                    'prov': [{'page_no': prov[0].get('page_no'), 'bbox': deepcopy(cell.get('bbox'))}]})
        streams = [items, structured] if document.get('m2_text_lines') else [structured]
        column_keys = {'subtotal': 'subtotal', 'sub total': 'subtotal',
            'tax amount': 'tax_amount', 'total tax': 'tax_amount',
            'vat amount': 'tax_amount', 'vat': 'tax_amount',
            'net worth': 'total_excluding_tax', 'gross worth': 'total_including_tax',
            'total excluding tax': 'total_excluding_tax', 'total excl. tax': 'total_excluding_tax',
            'total including tax': 'total_including_tax', 'total incl. tax': 'total_including_tax',
            'total amount': 'total_amount', 'grand total': 'total_amount', 'amount due': 'amount_due'}
        ambiguous_names = {'net worth', 'gross worth', 'vat'}
        symbols = {'RM': 'MYR', 'Rp': 'IDR', '€': 'EUR', '£': 'GBP', '₹': 'INR'}
        units = '|'.join(sorted(CURRENCY_MINOR_UNITS, key=len, reverse=True)) + r'|RM|Rp|€|£|₹|\$'
        amount_pattern = re.compile(r'\s*(?:(?P<prefix>' + units + r')\s*)?'
            r'(?P<number>-?\d[\d., \u00a0]*)\s*(?P<suffix>' + units + r')?\s*')

        def summary_number(text):
            match = amount_pattern.fullmatch(text)
            if not match or match['prefix'] and match['suffix']:
                raise ValueError('Not one printed monetary value')
            number = match['number'].strip().replace('\u00a0', ' ')
            basis = 'printed_decimal_point'
            if re.fullmatch(r'-?(?:\d+|\d{1,3}(?:[ .]\d{3})+),\d{2}', number):
                number = number.replace(' ', '').replace('.', '').replace(',', '.')
                basis = 'printed_decimal_comma'
            elif re.fullmatch(r'-?\d{1,3}(?:[, ]\d{3})+(?:\.\d{1,2})?', number):
                number = number.replace(',', '').replace(' ', '')
            value = format(Decimal(decimal_text(number)), '.2f')
            qualifier = match['prefix'] or match['suffix']
            code = symbols.get(qualifier, qualifier) if qualifier != '$' else None
            return value, qualifier, code, basis

        for stream in streams:
            positioned = []
            for item in stream:
                ev = evidence(item)
                if len(ev) == 1 and ev[0].get('bbox'):
                    box = ev[0]['bbox']
                    if box['r'] > box['l'] and box['b'] > box['t']:
                        positioned.append((item, ev[0]['page'], box))
            visited = set()
            for header, page, hb in positioned:
                name = header.get('text', '').strip().casefold()
                if name not in column_keys or header['self_ref'] in visited:
                    continue
                h = hb['b'] - hb['t']
                peers = [(x, b) for x, p, b in positioned if p == page
                    and abs((b['t'] + b['b'] - hb['t'] - hb['b']) / 2) <= h * .7]
                if any(re.fullmatch(r'description|qty|quantity|unit price|net price|price/unit',
                                   x.get('text', '').strip(), re.I) for x, _ in peers):
                    continue  # An item table's amounts are not document headers.
                columns = [(x, b, x.get('text', '').strip().casefold()) for x, b in peers
                           if x.get('text', '').strip().casefold() in column_keys]
                if len(columns) < 2:
                    continue
                visited.update(x['self_ref'] for x, _, _ in columns)
                anchors = [(x, b) for x, p, b in positioned if p == page
                    and re.fullmatch(r'summary\s*:?|financial summary\s*:?', x.get('text', '').strip(), re.I)
                    and 0 <= hb['t'] - b['b'] <= h * 8]
                footers = [(x, b) for x, p, b in positioned if p == page
                    and re.fullmatch(r'total(?: amount(?: due)?)?\s*:?', x.get('text', '').strip(), re.I)
                    and hb['b'] < b['t'] <= hb['b'] + h * 12
                    and b['r'] < min(box['l'] for _, box, _ in columns)]
                context = bool(anchors and footers and
                    {'net worth', 'vat', 'gross worth'} <= {n for _, _, n in columns})
                summary_refs.update(x['self_ref'] for x, _, _ in columns)
                summary_refs.update(x['self_ref'] for x, _ in footers)
                for col, cb, col_name in columns:
                    if col_name in ambiguous_names and not context:
                        continue
                    # Alias columns require an explicit aggregate row. Explicit
                    # amount labels may instead have a single value beneath them.
                    rows = footers if footers else [(col, cb)]
                    for footer, fb in rows:
                        for value_item, p, vb in positioned:
                            if p != page or value_item['self_ref'] in summary_refs:
                                continue
                            if abs(vb['r'] - cb['r']) > max(h, vb['b'] - vb['t']) * 1.5:
                                continue
                            if footers:
                                aligned = abs((vb['t'] + vb['b'] - fb['t'] - fb['b']) / 2) <= max(h, fb['b'] - fb['t'])
                            else:
                                aligned = 0 <= vb['t'] - cb['b'] <= h * 2
                            if not aligned:
                                continue
                            try:
                                value, qualifier, code, basis = summary_number(value_item.get('text', ''))
                            except ValueError:
                                continue
                            sources = [col, footer, value_item] if footers else [col, value_item]
                            sources = list({x['self_ref']: x for x in sources}.values())
                            raw = '\n'.join(x['text'] for x in sources)
                            targets = [column_keys[col_name]]
                            # Gross + explicit Total proves document total, not
                            # settlement. Total Amount Due remains amount_due.
                            if col_name == 'gross worth' and footers:
                                targets.append('amount_due' if re.search(r'\bdue\b', footer['text'], re.I) else 'total_amount')
                            for key in targets:
                                add(key, raw, (code + ' ' if code else '') + value, sources)
                                for ev in found[key][-1]['evidence']:
                                    ev.update(financial_summary_role=col_name,
                                              currency_qualifier=qualifier, normalization_basis=basis)
                            if code:
                                add('currency', value_item['text'], code, [value_item])

    # Preserve explicit block parsing, while comparing separately printed labels.
    for item in items:
        text = item.get('text', '').strip()
        if tax_header(item):
            continue
        if re.match(r'^customer\s+account(?=$|[\s:#])', text, re.I):
            continue
        for label, key in sorted(labels.items(), key=lambda pair: len(pair[0]), reverse=True):
            if key in party_details:
                # Explicit colon values were parsed with character spans by the
                # mapper. Standalone party blocks are handled together below.
                continue
            if key in money_keys and item.get('self_ref') in summary_refs:
                continue
            if any(c.get('value') and e.get('source_ref') == item['self_ref'] for c in found.get(key, []) for e in c.get('evidence', [])):
                continue
            if key in money_keys and item_header(item):
                continue
            pattern = re.escape(label).replace(r'\ ', r'\s+')
            pattern += r'(?:\s*\((?:MYR|RM|EUR|IDR|USD|SGD|GBP)\))?'
            standalone = re.fullmatch(pattern + r'\s*[:：#]?', text, re.I)
            match = None if standalone else re.fullmatch(pattern + r'\s*(?:[:：#]\s*|\s+)(\S[\s\S]*)', text, re.I)
            if match:
                value = match[1].strip()
                if key in {'invoice_date', 'bill_date', 'transaction_date', 'due_date'}:
                    value = re.split(r'\s+(?:hora|time)\s*[:：]', value, flags=re.I)[0].strip()
                if not plausible(key, value):
                    continue
                add(key, text, value, [item])
                break
            if not standalone:
                continue
            here = evidence(item)
            if len(here) != 1 or not here[0].get('bbox'):
                break
            box, page = here[0]['bbox'], here[0]['page']
            if key in party_keys and (box['r'] <= box['l'] or box['b'] <= box['t']):
                break
            height = max(1, box['b'] - box['t'])
            right_limit=float('inf')
            for other in items:
                if other is item or other.get('text','').strip().casefold().rstrip(':. ') not in label_names:continue
                there=evidence(other)
                if len(there)!=1 or not there[0].get('bbox') or there[0]['page']!=page:continue
                b=there[0]['bbox']
                if b['l']>=box['r'] and abs((box['t']+box['b']-b['t']-b['b'])/2)<=height*.65:
                    right_limit=min(right_limit,b['l'])
            right, underneath = [], []
            for other in items:
                if other is item or not other.get('text', '').strip() or not plausible(key, other['text']):
                    continue
                there = evidence(other)
                if len(there) != 1 or there[0].get('page') != page or not there[0].get('bbox'):
                    continue
                b = there[0]['bbox']
                if key in party_keys and (b['r'] <= b['l'] or b['b'] <= b['t']):
                    continue
                other_height = max(1, b['b'] - b['t'])
                center_delta = abs((b['t'] + b['b'] - box['t'] - box['b']) / 2)
                on_right = (center_delta <= min(height, other_height) * .65
                            and b['l'] >= box['r'] - height*.5 and b['r']>box['r'] and b['l']<right_limit)
                below = (-height*.2 <= b['t'] - box['b'] <= height * 1.4
                         and min(b['r'],box['r']) > max(b['l'],box['l'])
                         and (abs(b['l']-box['l']) <= height or abs(b['r']-box['r']) <= height))
                if key in party_keys:
                    # A party label names a column, not the other party across the page.
                    on_right = on_right and 0 <= b['l'] - box['r'] <= height * 4
                    below = (0 <= b['t'] - box['b'] <= height * 1.8
                             and abs(b['l'] - box['l']) <= height * 1.5
                             and b['l'] < right_limit and b['r'] <= right_limit)
                if on_right:
                    right.append(other)
                elif below:
                    underneath.append(other)
            if underneath:
                nearest=min(evidence(other)[0]['bbox']['t'] for other in underneath)
                underneath=[other for other in underneath if evidence(other)[0]['bbox']['t']-nearest<=height*.4]
            # Two plausible neighbours stay unresolved instead of picking one.
            neighbours = right + underneath if key in party_keys else (underneath if key == 'buyer_name' else right or underneath)
            for other in neighbours:
                value, sources = other['text'].strip(), [item, other]
                if key in party_keys:
                    # Retained lines can split a legal suffix from a multiline name.
                    # Extend only explicit suffixes, never an arbitrary address/name.
                    ob = evidence(other)[0]['bbox']
                    suffixes = []
                    for suffix in items:
                        if not re.fullmatch(r'(?:Sdn\.?\s+Bhd\.?|Pvt\.?\s+Ltd\.?|Ltd\.?|Limited|Inc\.?|LLC)', suffix.get('text', '').strip(), re.I):
                            continue
                        se = evidence(suffix)
                        if len(se) == 1 and se[0].get('bbox') and se[0]['page'] == page:
                            sb = se[0]['bbox']; sh = ob['b'] - ob['t']
                            if 0 <= sb['t'] - ob['b'] <= sh and abs(sb['l'] - ob['l']) <= sh:
                                suffixes.append(suffix)
                    if len(suffixes) == 1:
                        value += '\n' + suffixes[0]['text'].strip()
                        sources += suffixes
                add(key, '\n'.join(source['text'] for source in sources), value, sources)
            break

    if kind == 'invoice':
        located = [(item, party_position(item)) for item in items]
        located = [(item, ev) for item, ev in located if ev]
        roles = []
        for item, ev in located:
            match = re.fullmatch(r'(seller|supplier|vendor|client|customer|buyer|bill\s+to)\s*:\s*([^\n:]*)',
                                 item.get('text', '').strip(), re.I)
            if match:
                side = 'supplier' if match[1].casefold() in {'seller','supplier','vendor'} else 'customer'
                roles.append((side, item, ev))
        stop = r'(?:tax\s*id|gstin|tin|sst|vat|iban|bank|tel|telephone|phone|fax|email|e-mail|website)\b'
        section = r'(?:items|summary|description|particulars|quantity|qty|payment(?:\s+details)?|bank\s+details)\s*:?(?:\s.*)?'
        address_start = r'(?:\d|(?:plot|lot|unit|suite|floor|level|no\.?|p\.?\s*o\.?\s+box)\b)'
        street = r'\b(?:street|road|avenue|lane|drive|jalan|persiaran|lorong)\b|\bbox\s+\d+\b'
        for side, role, anchor in roles:
            name_key = 'supplier_name' if side == 'supplier' else 'buyer_name'
            names = [c for c in found.get(name_key, []) if c['value']]
            if len({c['value'] for c in names}) != 1:
                continue
            linked = [c for c in names if any(e.get('source_ref') == role['self_ref'] for e in c['evidence'])]
            if not linked:
                continue
            refs = {e['source_ref'] for c in linked for e in c['evidence']}
            name_sources = [(item, ev) for item, ev in located if item['self_ref'] in refs]
            if not name_sources or len(name_sources) != len(refs) or any(ev['page'] != anchor['page'] for _, ev in name_sources):
                continue
            box = anchor['bbox']; height = max(ev['bbox']['b']-ev['bbox']['t'] for _, ev in name_sources)
            bottom = max(ev['bbox']['b'] for _, ev in name_sources)
            left = min(ev['bbox']['l'] for _, ev in name_sources)
            right = anchor['page_size']['width']
            ceiling = bottom + height * 9
            for _, other, oe in roles:
                if other is role or oe['page'] != anchor['page']:
                    continue
                b = oe['bbox']
                if abs(b['t']-box['t']) <= height and b['l'] > box['r']:
                    right = min(right, b['l'])
                elif b['t'] > bottom and abs(b['l']-left) <= height * 1.5:
                    ceiling = min(ceiling, b['t'])
            region = []
            for item, ev in located:
                b = ev['bbox']; text = item.get('text', '').strip()
                if (item['self_ref'] in refs or ev['page'] != anchor['page']
                        or b['t'] < bottom-height*.2 or b['t'] >= ceiling
                        or abs(b['l']-left) > height*1.5 or b['r'] > right):
                    continue
                if re.fullmatch(section, text, re.I):
                    ceiling = min(ceiling, b['t'])
                region.append((item, ev))
            region = sorted([(item, ev) for item, ev in region if ev['bbox']['t'] < ceiling],
                            key=lambda pair: (pair[1]['bbox']['t'], pair[1]['bbox']['l']))
            address = []
            previous_bottom = bottom
            for item, ev in region:
                text = item.get('text', '').strip(); b = ev['bbox']
                if (re.match(stop, text, re.I) or ':' in text or not re.search(r'[A-Za-z]', text)
                        or b['t']-previous_bottom > height*1.2):
                    break
                if not address and not (re.match(address_start, text, re.I) or re.search(street, text, re.I)):
                    break
                address.append((item, ev)); previous_bottom = max(previous_bottom, b['b'])
            if address:
                conflicts = any(abs((a['bbox']['t']+a['bbox']['b']-b['bbox']['t']-b['bbox']['b'])/2)
                                < min(a['bbox']['b']-a['bbox']['t'], b['bbox']['b']-b['bbox']['t'])*.6
                                and x['text'].strip() != y['text'].strip()
                                for i,(x,a) in enumerate(address) for y,b in address[i+1:])
                sources = [role] + [item for item, _ in name_sources if item is not role]
                if conflicts:
                    # Keep competing printed lines as candidates, not a guessed
                    # concatenation that could silently mix overlapping blocks.
                    for item, _ in address:
                        add(side+'_address', item['text'], item['text'].strip(), sources+[item])
                else:
                    raw = '\n'.join(dict.fromkeys(item['text'].strip() for item, _ in address))
                    value = '\n'.join(dict.fromkeys(party_text(item) for item, _ in address))
                    add(side+'_address', raw, value, sources+[item for item, _ in address])
            for item, ev in region:
                match = re.fullmatch(r'tax\s*(?:id|identifier)\s*:\s*(.*)', item.get('text', '').strip(), re.I)
                if not match:
                    continue
                values = [(match[1].strip(), [item])] if match[1].strip() else []
                if not values:
                    a = ev['bbox']; h = a['b']-a['t']
                    for other, oe in located:
                        b = oe['bbox']; value = other.get('text', '').strip()
                        if (other is item or not tax_identifier(value) or oe['page'] != anchor['page']
                                or b['l'] < left-height or b['r'] > right or b['t'] >= ceiling):
                            continue
                        if (abs((a['t']+a['b']-b['t']-b['b'])/2) <= h*.6 and 0 <= b['l']-a['r'] <= h*4
                                or 0 <= b['t']-a['b'] <= h and abs(b['l']-a['l']) <= h):
                            values.append((value,[item,other]))
                for value, sources in values:
                    if tax_identifier(value):
                        add(side+'_tax_id', '\n'.join(s['text'] for s in sources), value, [role]+sources)

    if not found.get('currency'):
        currency_rows = []
        for item in items:
            text = item.get('text', '')
            for token, code in [(r'\b(?:MYR|RM)\b', 'MYR'), (r'\b(?:IDR|Rp)\b', 'IDR'), (r'\b(?:EUR|Euros?)\b|€', 'EUR')]:
                if re.search(token, text):
                    currency_rows.append((code, item))
        for code, item in currency_rows:
            add('currency', item['text'], code, [item])

    date_key = {
        'invoice': 'invoice_date',
        'bill': 'bill_date',
        'receipt': 'transaction_date'
    }.get(kind)

    if date_key and not found.get(date_key):
        for item in items:
            text = item.get('text', '').strip()

            # Do not mistake a bill's payment due date
            # for its document issue date.
            if kind == 'bill' and re.search(
                r'\b(?:due\s+date|date\s+due|payment\s+due|tarikh\s+akhir)\b',
                text,
                re.IGNORECASE,
            ):
                continue

            iso = re.findall(
                r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',
                text
            )
            bare = re.fullmatch(
                r'\d{1,2}[/.-]\d{1,2}[/.-]\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?',
                text
            )

            for raw in iso or ([text] if bare else []):
                try:
                    add(date_key, text, normalized_date(raw), [item])
                except ValueError:
                    pass

    supplier_key = {'invoice': 'supplier_name', 'bill': 'issuer', 'receipt': 'merchant'}.get(kind)
    if kind == 'receipt':
        positioned_receipt = [(item, party_position(item)) for item in items]
        positioned_receipt = [(item, ev) for item, ev in positioned_receipt if ev]
        other_roles = r'customer|buyer|bill\s+to|ship\s+to|delivery|shipping|payment|bank'
        metadata = r'^(?:tel|phone|fax|email|gst|sst|roc|reg(?:istration)?|cashier|date|tarikh|table|receipt|invoice|bill|transaction|qty|quantity|item|description|total|grand total|net total|' + other_roles + r')\b'
        transaction = (r'^(?:(?:receipt|invoice|bill|transaction)\s*(?:no\.?|number|#)|date\b|tarikh\b|cashier\b|'
                       r'(?:transaction\s+)?time\b|(?:qty|quantity)(?:\s+(?:description|item))?\s*:?\s*$|item\s*$|(?:' + other_roles + r')\b)')
        names = []
        heading_texts = {x.get('text','').strip() for x in document.get('texts',[])
                         if x.get('label') in {'title','section_header'}}
        for item, ev in positioned_receipt:
            text = item.get('text', '').strip(); box = ev['bbox']; h = box['b']-box['t']
            anchors = [oe['bbox']['t'] for other, oe in positioned_receipt
                       if oe['page']==ev['page'] and re.match(transaction, other.get('text','').strip(),re.I)]
            if not anchors or box['b'] > min(anchors):continue
            if re.match(metadata,text,re.I) or re.fullmatch(r'\(?[0-9][A-Za-z0-9-]*\)?',text):continue
            legal = re.search(r'\b(?:SDN\.?\s*BHD\.?|S/B|LTD\.?|ENTERPRISE)\b',text,re.I)
            address_below = any(oe['page']==ev['page'] and 0 <= oe['bbox']['t']-box['b'] <= h*1.5
                and re.match(r'^(?:\d+[A-Za-z]?(?:[-/, ]|$)|no\.?\s|lot\b|level\b|jalan\b)',other.get('text','').strip(),re.I)
                for other,oe in positioned_receipt if other is not item)
            if not legal and not ((item.get('label') in {'title','section_header'} or text in heading_texts) and address_below):continue
            value = re.sub(r'\s*\([0-9][A-Za-z0-9-]*\)\s*$', '', text).strip()
            if legal or address_below:names.append((item,ev,value))
        if names and not found.get('merchant'):
            for item,ev,value in names:add('merchant',item['text'],value,[item])
        if len({c['value'] for c in found.get('merchant',[]) if c['value']}) == 1:
            values = {c['value'] for c in found['merchant'] if c['value']}
            for name, ne, value in names:
                if value not in values:continue
                nb=ne['bbox']; h=nb['b']-nb['t']; bottom=nb['b']; address=[]
                header = sorted([(item,ev) for item,ev in positioned_receipt if ev['page']==ne['page']
                    and item is not name and ev['bbox']['t']>=nb['b']-h*.25
                    and abs((ev['bbox']['l']+ev['bbox']['r']-nb['l']-nb['r'])/2)<=max((nb['r']-nb['l'])*.4,h*2)],
                    key=lambda pair:pair[1]['bbox']['t'])
                for item,ev in header:
                    text=item.get('text','').strip();box=ev['bbox']
                    if box['t']-bottom>h*1.8 or box['t']>nb['b']+h*8:break
                    if re.fullmatch(r'\(?[0-9][A-Za-z0-9-]*\)?',text) and not address:
                        bottom=box['b'];continue
                    if re.match(metadata,text,re.I) or ':' in text:break
                    if address and re.match(r'^\d{2,}[-/][A-Za-z]',text):break # branch reference, not a street continuation
                    if not address and not re.match(r'^(?:\d|no\.?\s|lot\b|level\b|jalan\b|unit\b)',text,re.I):break
                    if not re.search(r'[A-Za-z]',text):break
                    address.append(item);bottom=max(bottom,box['b'])
                if address:
                    raw='\n'.join(item['text'].strip() for item in address)
                    add('merchant_address',raw,raw,[name]+address)

        # Receipt dates are read from an explicit label or an isolated printed
        # timestamp. Never infer a century for a two-digit year or locale for
        # a day/month pair that is ambiguous under the existing date policy.
        for item,ev in positioned_receipt:
            if not re.fullmatch(r'(?:receipt|bill|invoice)\s*(?:no\.?|number|#)\s*:?',item.get('text','').strip(),re.I):continue
            a=ev['bbox'];h=a['b']-a['t']
            for other,oe in positioned_receipt:
                b=oe['bbox']
                ref=re.fullmatch(r'\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9./_-]{0,99})',other.get('text',''))
                if (ref and oe['page']==ev['page'] and b['l']>=a['r']
                        and abs((a['t']+a['b']-b['t']-b['b'])/2)<=h*.65):
                    add('receipt_number',item['text']+'\n'+other['text'],ref[1],[item,other])
        for item,ev in positioned_receipt:
            text=item.get('text','').strip()
            match=re.fullmatch(r'(?:(?:transaction date|date|tarikh)\s*:?\s*|:\s*)?(\d{1,2}[/.-]\d{1,2}[/.-]\d{2}(?:\d{2})?)(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?',text,re.I)
            if match:
                raw=match[1]
                try:value=normalized_date(raw)
                except ValueError:value=raw
                b=ev['bbox'];h=b['b']-b['t']
                labels=[other for other,oe in positioned_receipt if oe['page']==ev['page']
                    and re.fullmatch(r'(?:date|tarikh)\s*:?',other.get('text','').strip(),re.I)
                    and oe['bbox']['r']<=b['l']+h and abs((oe['bbox']['t']+oe['bbox']['b']-b['t']-b['b'])/2)<=h*.65]
                for candidate in found.get('transaction_date',[]):
                    if any(e.get('source_ref')==item['self_ref'] for e in candidate['evidence']):
                        candidate['value']=value  # Same printed date; retain its original timestamp/raw text.
                add('transaction_date',text,value,labels+[item])

        tax_sections = [(ev['page'],ev['bbox']['t']) for item,ev in positioned_receipt
                        if re.fullmatch(r'(?:gst|tax)\s+summary',item.get('text','').strip(),re.I)]
        def before_tax(ev):
            return not any(page==ev['page'] and ev['bbox']['t']>=top for page,top in tax_sections)
        total_rows=[]
        for item,ev in positioned_receipt:
            text=item.get('text','').strip();a=ev['bbox'];h=a['b']-a['t']
            label=re.fullmatch(r'((?:(?:grand|net)\s+)?total(?:\s+amount)?|total\s+(?:rounded|amt\s+payable)|total\s+sales\s*\(inclusive\s+gst\))\s*:?(?:\s*(?:RM|MYR))?',text,re.I)
            if not label or not before_tax(ev):continue
            for other,oe in positioned_receipt:
                b=oe['bbox'];value=other.get('text','').strip()
                if oe['page']!=ev['page'] or b['l']<a['r'] or abs((a['t']+a['b']-b['t']-b['b'])/2)>max(h,b['b']-b['t'])*.65:continue
                try:value=normalized_amount(value,None)
                except ValueError:continue
                total_rows.append((2 if re.match(r'grand|net|total\s+(?:rounded|amt\s+payable)',label[1],re.I) else 1,item,other,value))
        if total_rows:
            rank=max(row[0] for row in total_rows)
            # Tax-summary aggregates and intermediate subtotals are not competing
            # document totals. Keep all equally final candidates for review.
            selected=[row for row in total_rows if row[0]==rank]
            refs={x['self_ref'] for _,label,value,_ in selected for x in (label,value)}
            found['total_amount'][:]=[c for c in found.get('total_amount',[]) if any(e.get('source_ref') in refs for e in c['evidence'])]
            for _,label,amount,value in selected:add('total_amount',label['text']+'\n'+amount['text'],value,[label,amount])
        for item,ev in positioned_receipt:
            if not re.match(r'^(?:cash|tunai)\b',item.get('text','').strip(),re.I):continue
            a=ev['bbox'];h=a['b']-a['t']
            for other,oe in positioned_receipt:
                b=oe['bbox']
                if oe['page']!=ev['page'] or b['l']<a['r'] or abs((a['t']+a['b']-b['t']-b['b'])/2)>h*.65:continue
                try:normalized_amount(other.get('text',''),None)
                except ValueError:continue
                add('payment_method',item['text'],'cash',[item,other])
    if supplier_key and kind != 'receipt' and not found.get(supplier_key) and not (kind == 'invoice' and has_party_role):
        # Receipts require the bounded merchant header above; a free-standing
        # company regex cannot establish merchant ownership or preserve its name.
        # Legal-entity headings near the top are stronger than arbitrary first
        # lines/logos. Never promote an address or a staff/customer line.
        for item in items[:30]:
            text = item.get('text', '').strip()
            positions = evidence(item)
            if kind == 'receipt' and (len(positions) != 1 or not positions[0].get('bbox')):
                continue
            if positions and positions[0].get('bbox') and positions[0].get('page_size'):
                if positions[0]['page'] != 1 or positions[0]['bbox']['t'] > positions[0]['page_size']['height'] * .4:
                    continue
            # OCR can split a legal suffix into a distinct aligned region.
            suffixes = []
            if not re.search(r'\b(?:LDA|LTD|LIMITED|ENTERPRISE|SDN\s+BHD|S\.?A\.?)\b', text, re.I):
                for other in items[:30]:
                    if re.fullmatch(r'S\.?\s*A\.?', other.get('text','').strip(), re.I):
                        there = evidence(other)
                        if len(positions) == len(there) == 1 and positions[0].get('bbox') and there[0].get('bbox') and positions[0]['page'] == there[0]['page']:
                            a, b = positions[0]['bbox'], there[0]['bbox']
                            h = min(a['b']-a['t'], b['b']-b['t'])
                            if abs((a['t']+a['b']-b['t']-b['b'])/2) < h*.65 and 0 <= b['l']-a['r'] < h*2:
                                suffixes.append(other)
            if len(suffixes) == 1:
                text += ' ' + suffixes[0]['text']
            company = re.match(r'^(.{2,100}?\b(?:Sdn\s*\.?\s*Bhd\.?|LDA|LIMITED|LTD|ENTERPRISE|S\.?\s*A\.?))\b', text, re.I)
            if not company:
                company = re.search(r'(?<![A-Za-z])([A-Z][A-Z ()&.\x27-]{2,80}\s+SDN\s+BHD)\b', text)
            if company and not re.search(r'\b(?:buyer|customer|bill to|ship to|cliente|consumidor)\b', company[1], re.I):
                add(supplier_key, text, company[1].strip(), [item]+suffixes)

    if kind == 'bill' and not found.get('bill_reference'):
        for i, item in enumerate(items):
            if item.get('text','').strip().casefold() != 'consulta':
                continue
            for other in items[i+1:i+3]:
                match = re.fullmatch(r'(?:FS|FT)\s+\d+/[\w-]+', other.get('text','').strip(), re.I)
                if match:
                    add('bill_reference', other['text'], match[0], [item, other])

    if kind == 'receipt' and not found.get('payment_method'):
        for item in items:
            if not re.fullmatch(r'cash\s*:?', item.get('text','').strip(), re.I):continue
            here=evidence(item)
            if len(here)!=1 or not here[0].get('bbox'):continue
            a=here[0]['bbox'];h=a['b']-a['t']
            for other in items:
                if not re.fullmatch(r'\d+\.\d{2}',other.get('text','').strip()):continue
                there=evidence(other)
                if len(there)==1 and there[0].get('bbox') and there[0]['page']==here[0]['page']:
                    b=there[0]['bbox']
                    if b['l']>=a['r'] and abs((a['t']+a['b']-b['t']-b['b'])/2)<=h*.65:
                        add('payment_method',item['text'],'cash',[item,other])

    if kind=='receipt' and not found.get('tax_amount'):
        for label in items:
            if label.get('text','').strip().casefold().rstrip(':')!='tax inclusive': continue
            le=evidence(label)
            if len(le)!=1 or not le[0].get('bbox'): continue
            a=le[0]['bbox'];h=a['b']-a['t']
            for rate in items:
                if not re.fullmatch(r'\d+(?:\.\d+)?%\s+of\s+(?:RM|MYR)\s*\d+(?:\.\d+)?',rate.get('text','').strip(),re.I):continue
                re_=evidence(rate)
                if len(re_)!=1 or not re_[0].get('bbox') or re_[0]['page']!=le[0]['page']:continue
                b=re_[0]['bbox']
                if not a['t']<b['t']<a['b']+h*2:continue
                for amount in items:
                    ae=evidence(amount)
                    if len(ae)!=1 or not ae[0].get('bbox') or ae[0]['page']!=le[0]['page']:continue
                    c=ae[0]['bbox']
                    if c['l']>b['r'] and abs((b['t']+b['b']-c['t']-c['b'])/2)<h*.7 and re.fullmatch(r'(?:RM|MYR)\s*\d+\.\d{2}',amount.get('text','').strip()):
                        add('tax_amount',amount['text'],amount['text'],[label,rate,amount])

    if kind == 'invoice' and not found.get('invoice_number'):
        for item in items:
            match = re.search(r'\b(?:Fatura Simplificada|Fatura|Factura)\s+Nr[.:º]*\s*:?\s*((?:FS|FT)\s+\d+/[\w-]+)', item.get('text', ''), re.I)
            if match:
                add('invoice_number', item['text'], match[1], [item])

    if kind == 'invoice':
        # A footer aggregate row may repeat the amounts under the item table's
        # named columns. Never treat an individual item's subtotal/tax as a
        # document header merely because it sits below a column heading.
        column_names = {'sub total':'subtotal', 'subtotal':'subtotal', 'tax amt':'tax_amount',
                        'tax amount':'tax_amount', 'total incl. tax':'total_including_tax',
                        'total excl. tax':'total_excluding_tax'}
        for footer in items:
            if not re.fullmatch(r'total amount due\s*:?',footer.get('text','').strip(),re.I):continue
            fe=evidence(footer)
            if len(fe)!=1 or not fe[0].get('bbox'):continue
            fb=fe[0]['bbox'];fh=fb['b']-fb['t']
            for header in items:
                name=re.sub(r'\s*\((?:RM|MYR)\)\s*$','',header.get('text','').strip(),flags=re.I).casefold()
                key=column_names.get(name)
                if not key or not item_header(header):continue
                he=evidence(header)
                if len(he)!=1 or not he[0].get('bbox') or he[0]['page']!=fe[0]['page']:continue
                hb=he[0]['bbox'];hh=hb['b']-hb['t']
                if hb['b']>=fb['t']:continue
                matches=[]
                for other in items:
                    if not plausible('total_amount',other.get('text','')):continue
                    oe=evidence(other)
                    if len(oe)!=1 or not oe[0].get('bbox') or oe[0]['page']!=fe[0]['page']:continue
                    b=oe[0]['bbox']
                    if abs((b['t']+b['b']-fb['t']-fb['b'])/2)<=fh*.65 and abs(b['r']-hb['r'])<=max(hh,fh):
                        matches.append(other)
                for other in matches:
                    add(key,footer['text']+'\n'+header['text']+'\n'+other['text'],other['text'].strip(),[header,footer,other])

    currencies = {c['value'] for c in found.get('currency', [])}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    dates = {'invoice_date', 'bill_date', 'transaction_date', 'due_date'}
    money = money_keys
    for key, candidates in found.items():
        for candidate in candidates:
            try:
                if key in dates:
                    candidate['value'] = normalized_date(candidate['value'])
                elif key in money:
                    summary_value = re.fullmatch(r'([A-Z]{3}) (-?\d+(?:\.\d+)?)', candidate['value'])
                    if summary_value and any(e.get('financial_summary_role') for e in candidate['evidence']):
                        # Canonical digits were parsed from this printed cell.
                        # Remove its ISO qualifier only after currency agreement.
                        if summary_value[1] == currency:
                            candidate['value'] = summary_value[2]
                        continue
                    # Never erase a conflicting qualifier before the typed
                    # mapper can mark it unresolved. Currency may itself be
                    # ambiguous because the source prints several codes.
                    qualifiers = re.findall(r'\b(?:MYR|RM|IDR|Rp|EUR|USD|SGD|GBP)\b', candidate['value'])
                    codes = {'RM': 'MYR', 'Rp': 'IDR'}
                    if any(codes.get(token, token) != currency for token in qualifiers):
                        continue
                    candidate['value'] = normalized_amount(candidate['value'], currency)
                elif key=='payment_method' and candidate['value'].strip().casefold() in {'visa','mastercard','master card','amex','american express'}:
                    # Normalize an explicit card network to the existing M3
                    # method enum; this never implies payment/settlement status.
                    candidate['value']='card'
            except ValueError:
                # The main mapper converts this raw unsupported value into a
                # null/ambiguous field, retaining every candidate and its evidence.
                pass

    # A complete printed tax breakdown is distinct from item/summary totals.
    # Recover a printed single row, or a labelled multi-row aggregate only
    # when its gross amounts reconcile exactly with an explicit document total.
    if currency:
        tax_rows=[]
        for header in items:
            if header.get('text','').strip().casefold() not in {'iva%','taxa','tax rate'}:
                continue
            he=evidence(header)
            if len(he)!=1 or not he[0].get('bbox'):
                continue
            a=he[0]['bbox']; h=a['b']-a['t']; page=he[0]['page']
            columns={}
            for other in items:
                oe=evidence(other)
                if len(oe)!=1 or not oe[0].get('bbox') or oe[0]['page']!=page: continue
                b=oe[0]['bbox']
                if abs((a['t']+a['b']-b['t']-b['b'])/2)>h*.7: continue
                label=other.get('text','').strip().casefold()
                key={'base':'base','liquido':'base','líquido':'base','iva':'tax','tax':'tax','total':'gross'}.get(label)
                if key: columns.setdefault(key,[]).append((other,b))
            if set(columns)!={'base','tax','gross'} or any(len(v)!=1 for v in columns.values()): continue
            for rate in items:
                if not re.fullmatch(r'\d+(?:[.,]\d+)?%',rate.get('text','').strip()): continue
                re_=evidence(rate)
                if len(re_)!=1 or not re_[0].get('bbox') or re_[0]['page']!=page: continue
                rb=re_[0]['bbox']
                if not (a['t']<rb['t']<a['b']+h*4 and abs(rb['l']-a['l'])<h*2): continue
                cells={}
                for key,[(column,box)] in columns.items():
                    matches=[]
                    for other in items:
                        oe=evidence(other)
                        if len(oe)!=1 or not oe[0].get('bbox') or oe[0]['page']!=page: continue
                        b=oe[0]['bbox']
                        if abs((rb['t']+rb['b']-b['t']-b['b'])/2)<=min(h,b['b']-b['t'])*.65 and abs(b['r']-box['r'])<h:
                            try: matches.append((other,Decimal(normalized_amount(other['text'],currency))))
                            except ValueError: pass
                    if len(matches)==1: cells[key]=matches[0]
                if set(cells)=={'base','tax','gross'} and cells['base'][1]+cells['tax'][1]==cells['gross'][1]:
                    tax_rows.append(cells)
        totals={Decimal(c['value']) for c in found.get('total_amount',[]) if re.fullmatch(r'-?\d+(?:\.\d+)?',c['value'])}
        if tax_rows and (len(tax_rows)==1 or (len(totals)==1 and sum((row['gross'][1] for row in tax_rows),Decimal('0'))==next(iter(totals)))):
            for key,target in [('base','subtotal'),('tax','tax_amount')]:
                if found.get(target): continue
                sources=[row[key][0] for row in tax_rows]
                add(target,' + '.join(item['text'] for item in sources),str(sum((row[key][1] for row in tax_rows),Decimal('0'))),sources)
                if len(tax_rows)>1:
                    for ev in found[target][-1]['evidence']:
                        ev['derivation']='sum_of_complete_printed_tax_rows'
