"""Conservative item rows from retained OCR geometry. No inferred prices/quantities."""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import re

from services.extraction.contracts.accounting_contract import CURRENCY_MINOR_UNITS, decimal_text
from services.extraction.layout.layout_headers import records

DESCRIPTION = re.compile(r'^(?:description|item(?: description)?|product|particulars|details|charges?|(?:iva\s+)?descri[cç][aã]o|nome do artigo)$', re.I)
AMOUNT = re.compile(r'^(?:line total|line amount|amount|amt|total|valor|value|total incl\.? tax)(?:\s*\((?:RM|MYR)\))?$', re.I)
QUANTITY = re.compile(r'^(?:[qo]ty\.?|quantity|quantidade|qtd\.?)$', re.I)  # "Oty." is a recurring OCR read of "Qty."
PRICE = re.compile(r'^(?:unit price|unit cost|price|price/unit|s/price|rate|pre[cç]o(?: unit[aá]rio)?)$', re.I)
SUMMARY = re.compile(r'^(?:sub[-\s]*total(?:\s*\([^)]*\))?|grand total|total(?:\s+(?:a pagar|amount|due|eur|rm|myr))?|amount due|balance(?: due)?|previous balance|current charges|units sold|payment(?: method)?|cash|change|tax inclusive|tax summary|iva%|taxa|entregue|troco)\s*[:：]?(?:\s+.*)?$', re.I)
NUMBER = r'-?\d[\d.,]*'
MULTIPLIER = re.compile(r'^\s*(' + NUMBER + r')\s*[x×X]\s*(' + NUMBER + r')\s*$')
TAXED_AMOUNT = re.compile(NUMBER + r'(?:\s+[A-Z]{1,3})?')  # A printed tax code may follow a line amount (193.00 SR).
QTY_PRICE_CELL = re.compile(r'(\d+(?:\.\d+)?)\s+(\d+\.\d{2,4})')  # OCR can merge adjacent quantity and price cells.


def number(text, currency, scale=2, *, invoice=False):
    """Explicit locale/currency normalization, retaining exact Decimal precision."""
    value = text.strip()
    if invoice:
        units = '|'.join(CURRENCY_MINOR_UNITS) + r'|RM|Rp|€|£|₹|\$'
        match = re.fullmatch(r'(?:(?P<unit>' + units + r')\s*)?(?P<value>-?\d[\d., \u00a0]*?)'
                             r'(?:\s*(?P<suffix>' + units + r'))?', value)
        if not match or match['unit'] and match['suffix']:
            raise ValueError('Expected one printed numeric cell')
        unit = match['unit'] or match['suffix']
        if unit and scale==3:
            raise ValueError('A currency-qualified cell is not a quantity')
        code = {'RM':'MYR','Rp':'IDR','€':'EUR','£':'GBP','₹':'INR'}.get(unit,unit)
        if code and code != '$' and currency and code != currency:
            raise ValueError('Currency qualifier disagrees with document currency')
        value = match['value'].strip().replace('\u00a0',' ')
        if re.fullmatch(r'-?(?:\d+|\d{1,3}(?:[ .]\d{3})+),\d{1,2}',value):
            value=value.replace(' ','').replace('.','').replace(',','.')
        elif re.fullmatch(r'-?\d{1,3}(?:[, ]\d{3})+\.\d{1,'+str(scale)+r'}',value):
            value=value.replace(',','').replace(' ','')
        elif ',' in value or ' ' in value:
            raise ValueError('Ambiguous numeric grouping')
        return decimal_text(value,14,scale)
    qualifiers = re.findall(r'\b(?:RM|MYR|EUR|IDR|Rp|USD|SGD|GBP)\b', value)
    if any({'RM':'MYR','Rp':'IDR'}.get(token,token) != currency for token in qualifiers):
        raise ValueError('Currency qualifier disagrees with document currency')
    value = re.sub(r'^(?:RM|MYR|EUR|IDR|Rp\.?|USD|SGD|GBP)\s*', '', value)
    value = re.sub(r'\s*(?:EUR|Euros?|MYR|IDR|USD|SGD|GBP|€)$', '', value, flags=re.I)
    if currency == 'EUR' and ',' in value:
        if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:\.\d{3})+),\d{1,' + str(scale) + r'}', value):
            raise ValueError('Ambiguous decimal separator')
        value = value.replace('.', '').replace(',', '.')
    elif currency == 'IDR' and re.fullmatch(r'-?\d{1,3}(?:\.\d{3})+', value):
        value = value.replace('.', '')
    return decimal_text(value, 14, scale)


def recover_rows(document, kind, currency, location, field, issue):
    if kind == 'unknown':
        return []
    pages = defaultdict(list)
    for item in records(document):
        prov = item.get('prov', [])
        if len(prov) != 1 or item.get('content_layer', 'body') != 'body':
            continue
        p = prov[0]
        ev = location(document, p.get('page_no'), p.get('bbox'), item.get('self_ref'), item.get('text', ''))
        if not ev.get('bbox'):
            continue
        ev.update(from_ocr=item.get('from_ocr'), ocr_score=item.get('ocr_score'), bbox_scope='source_line')
        pages[p['page_no']].append({'text': item.get('text', '').strip(), 'ev': ev, 'box': ev['bbox']})

    def center(item):
        b = item['box']
        return (b['t'] + b['b']) / 2

    def aligned(a, b):
        return abs(center(a)-center(b)) <= min(a['box']['b']-a['box']['t'], b['box']['b']-b['box']['t']) * .65

    def cell(items, key, transform=None):
        candidates = []
        for item in items:
            raw = item['text']
            try:
                box=item['ev'].get('bbox')
                if kind == 'invoice' and (item.get('unsupported') or not box
                                         or box['r']<=box['l'] or box['b']<=box['t']):
                    raise ValueError('Unsupported cell span or source geometry')
                value = transform(raw) if transform else raw
            except ValueError:
                value = None
            candidates.append({'raw_text': raw, 'value': value, 'evidence': item.get('evidence', [item['ev']])})
        values = {c['value'] for c in candidates}
        if kind=='invoice' and key!='description':
            values={Decimal(v) if v is not None else None for v in values}
        resolved = len(values) == 1 and None not in values
        if not candidates:
            return field()
        if not resolved:
            issue('LINE_FIELD_REQUIRES_REVIEW', key + ': conflicting or unsupported source values')
        return field('\n'.join(c['raw_text'] for c in candidates), candidates[0]['value'] if resolved else None,
                     [e for c in candidates for e in c['evidence']], 'mapped' if resolved else 'ambiguous', candidates)

    def row(description, amounts, quantity=(), prices=()):
        return {'description': cell(description, 'description', lambda s: ' '.join((s if kind == 'invoice' else re.sub(r'^\d+(?:[.,]\d+)?%\s*', '', s)).split())),
                'line_total': cell(amounts, 'line_total', lambda s: number(s if kind=='invoice' else re.sub(r'\s+[A-Z]{1,3}$', '', s), currency, invoice=kind=='invoice')),
                'quantity': cell(quantity, 'quantity', lambda s: number(s, currency, 3, invoice=kind=='invoice')),
                'unit_price': cell(prices, 'unit_price', lambda s: number(s, currency, 4, invoice=kind=='invoice'))}

    def summary(text):
        if kind != 'invoice':
            return SUMMARY.fullmatch(text)
        return re.fullmatch(r'(?:financial summary|summary|sub\s*total|net total|grand total|total(?: amount(?: due)?)?|'
                            r'tax total|amount due|previous balance|current charges|payment(?: method)?|cash|change)'
                            r'\s*[:：]?(?:\s*(?:[A-Z]{3}|[$€£₹])?\s*-?\d[\d., ]*)?',text,re.I)

    def heading(text):
        if kind!='invoice':
            return text
        value=' '.join(text.casefold().split())
        if kind=='invoice':
            value=re.sub(r'\s*\((?:'+'|'.join(CURRENCY_MINOR_UNITS)+r'|RM|Rp)\)\s*$','',value,flags=re.I)
        return value

    def numeric(text):
        if kind != 'invoice':
            return re.fullmatch(NUMBER,text)
        try:
            number(text,currency,4,invoice=True)
            return True
        except ValueError:
            return False

    covered, structured = defaultdict(list), []
    if kind == 'invoice':
        # Keep reliable table cells even when the retained stream merges columns.
        # Extra numbered/unit/tax columns do not invalidate an item table.
        for ti, table in enumerate(document.get('tables', [])):
            data, prov = table.get('data',{}), table.get('prov',[])
            ref=table.get('self_ref',f'#/tables/{ti}')
            if len(prov)!=1 or not prov[0].get('page_no') or data.get('orientation','rot_0')!='rot_0':
                continue
            page=prov[0]['page_no']; cells=data.get('table_cells',[])
            heads=[(ci,c) for ci,c in enumerate(cells) if c.get('start_row_offset_idx')==0]
            roles=defaultdict(list)
            for ci,c in heads:
                name=heading(c.get('text',''))
                key=('description' if DESCRIPTION.fullmatch(name) else
                     'quantity' if QUANTITY.fullmatch(name) else
                     'unit_price' if PRICE.fullmatch(name) or name=='net price' else
                     'line_total' if AMOUNT.fullmatch(name) else
                     'net' if name in {'net','net worth','net amount','net total'} else
                     'gross' if name in {'gross','gross worth','gross amount'} else None)
                if key: roles[key].append((ci,c))
            if not roles['description'] or not any(roles[k] for k in ['line_total','net','gross','quantity','unit_price']):
                continue
            amount_role='line_total' if roles['line_total'] else 'net' if roles['net'] else 'gross'
            roles['line_total']=roles[amount_role]
            header_boxes=[location(document,page,c.get('bbox'),None,c.get('text','')).get('bbox') for _,c in heads]
            header_boxes=[b for b in header_boxes if b and b['r']>b['l'] and b['b']>b['t']]
            if not header_boxes:
                continue
            row_ids=sorted({c.get('start_row_offset_idx') for c in cells
                            if isinstance(c.get('start_row_offset_idx'),int) and c['start_row_offset_idx']>0})
            table_boxes=list(header_boxes)
            for ri in row_ids:
                source_row=[(ci,c) for ci,c in enumerate(cells) if c.get('start_row_offset_idx')==ri]
                if any(summary(c.get('text','').strip()) for _,c in source_row):
                    break  # Subsequent aggregate/payment rows are not items.
                groups={k:[] for k in ['description','quantity','unit_price','line_total']}
                for key in groups:
                    for hci,header in roles[key]:
                        column=header.get('start_col_offset_idx')
                        if not isinstance(column,int): continue
                        he=location(document,page,header.get('bbox'),f'{ref}/data/table_cells/{hci}',header.get('text',''))
                        for ci,c in source_row:
                            start=c.get('start_col_offset_idx')
                            if not isinstance(start,int) or not start<=column<start+c.get('col_span',1): continue
                            text=c.get('text','').strip()
                            if not text: continue
                            ev=location(document,page,c.get('bbox'),f'{ref}/data/table_cells/{ci}',c.get('text',''))
                            ev.update(bbox_scope='source_cell',from_ocr=c.get('from_ocr'),ocr_score=c.get('ocr_score'))
                            if ev.get('bbox'): table_boxes.append(ev['bbox'])
                            if key=='line_total':
                                ev['amount_basis']=('net_excluding_tax' if amount_role=='net' else
                                                    'printed_gross' if amount_role=='gross' else 'printed_line_amount')
                            ev['column_heading']=header.get('text','')
                            unsupported=(c.get('row_span',1)!=1 or c.get('col_span',1)!=1
                                         or header.get('col_span',1)!=1 or not he.get('bbox'))
                            if key=='line_total' and header.get('text','').strip().casefold() in {'net','gross'}:
                                unsupported |= not roles['unit_price'] and not any(re.search(r'\b(?:vat|tax)\b',h.get('text',''),re.I) for _,h in heads)
                            item={'text':text,'ev':ev,'evidence':[deepcopy(he),ev],
                                  'unsupported':unsupported,'box':ev.get('bbox')}
                            groups[key].append(item)
                if not any(groups.values()): continue
                if (groups['description'] and all(DESCRIPTION.fullmatch(x['text']) for x in groups['description'])
                        and any(pattern.fullmatch(x['text']) for key,pattern in
                                [('quantity',QUANTITY),('unit_price',PRICE),('line_total',AMOUNT)] for x in groups[key])):
                    continue  # Repeated column headers are not purchased items.
                # Row indices provide grouping; reject cells visibly displaced
                # from the description's first line rather than borrowing a row.
                anchors=[x['box'] for x in groups['description'] if x.get('box')]
                if anchors:
                    anchor=min(anchors,key=lambda b:b['t'])
                    for key in ['quantity','unit_price','line_total']:
                        for item in groups[key]:
                            b=item.get('box')
                            if b and abs(b['t']-anchor['t'])>max(12,(b['b']-b['t'])*1.2): item['unsupported']=True
                # Corroborate only a complete, co-located OCR cell. Merged
                # multi-column lines are not allowed to erase structured values.
                for key, members in groups.items():
                    extra=[]
                    for item in members:
                        a=item.get('box')
                        if not a or item['unsupported']: continue
                        for other in pages.get(page,[]):
                            b=other['box']; intersection=max(0,min(a['r'],b['r'])-max(a['l'],b['l']))*max(0,min(a['b'],b['b'])-max(a['t'],b['t']))
                            union=(a['r']-a['l'])*(a['b']-a['t'])+(b['r']-b['l'])*(b['b']-b['t'])-intersection
                            if union<=0 or intersection/union<.8: continue
                            if key!='description' and not numeric(other['text']): continue
                            if key=='description' and ' '.join(other['text'].split())!=' '.join(item['text'].split()):
                                issue('LINE_REPRESENTATION_DISAGREEMENT','Description differs in co-located source representations')
                            extra.append({**other,'evidence':[deepcopy(e) for e in item['evidence'][:1]]+[deepcopy(other['ev'])]})
                    members.extend(extra)
                boxes=[x['box'] for values in groups.values() for x in values if x.get('box')]
                if boxes:
                    bounds={k:(min if k in {'l','t'} else max)(b[k] for b in boxes) for k in ['l','t','r','b']}
                    duplicate=next((g for p,b,g in structured if p==page and all(abs(b[k]-bounds[k])<2 for k in bounds)),None)
                    if duplicate is not None:
                        for key in groups: duplicate[key].extend(groups[key])
                    else: structured.append((page,bounds,groups))
            covered[page].append({k:(min if k in {'l','t'} else max)(b[k] for b in table_boxes) for k in ['l','t','r','b']})

    result = [row(g['description'],g['line_total'],g['quantity'],g['unit_price']) for _,_,g in structured]
    for page, items in sorted(pages.items()):
        items.sort(key=lambda item: (item['box']['t'], item['box']['l']))
        headers = []
        for desc in items:
            if not DESCRIPTION.fullmatch(desc['text']):
                continue
            if any(b['l']<=desc['box']['l']<=b['r'] and b['t']<=center(desc)<=b['b'] for b in covered[page]):
                continue
            amounts = [x for x in items if x['box']['l'] > desc['box']['r'] and AMOUNT.fullmatch(heading(x['text'])) and aligned(desc, x)]
            if kind=='invoice' and not amounts:
                amounts=[x for x in items if x['box']['l']>desc['box']['r'] and re.fullmatch(r'net(?: worth| amount| total)?',x['text'],re.I) and aligned(desc,x)]
                if not amounts:
                    amounts=[x for x in items if x['box']['l']>desc['box']['r'] and re.fullmatch(r'gross(?: worth| amount)?',x['text'],re.I) and aligned(desc,x)]
            if len(amounts) == 1:
                headers.append((desc, amounts[0]))
        if headers:
            for hi, (desc, amount) in enumerate(headers):
                top = max(desc['box']['b'], amount['box']['b'])
                bottom = headers[hi+1][0]['box']['t'] if hi+1 < len(headers) else float('inf')
                summaries = [x['box']['t'] for x in items if x['box']['t'] >= top and x['box']['l'] < amount['box']['l'] and summary(x['text'])]
                bottom = min([bottom] + summaries)
                middle_headers = [x for x in items if aligned(desc,x) and desc['box']['l'] < x['box']['l'] < amount['box']['l']]
                qheaders = [x for x in middle_headers if QUANTITY.fullmatch(x['text'])]
                pheaders = [x for x in middle_headers if PRICE.fullmatch(heading(x['text'])) or kind=='invoice' and heading(x['text'])=='net price']
                desc_right = min([x['box']['l'] for x in qheaders+pheaders] + [amount['box']['l']])
                # OCR rectangles of the first data row can overlap the header's
                # bottom by a few pixels; retain that row without taking headers.
                slack=min(desc['box']['b']-desc['box']['t'],amount['box']['b']-amount['box']['t'])*.2
                body = [x for x in items if top-slack <= x['box']['t'] < bottom]
                # Some till slips print SKU/quantity/prices on one line and the
                # description underneath. Require the explicit Item/Qty/Amount
                # grid and printed code anchors; never borrow the next row's value.
                code = re.compile(r'(?=.*\d)[A-Z0-9/-]{3,32}$', re.I)
                anchors = [x for x in body if re.fullmatch(r'item|description',desc['text'],re.I) and qheaders
                           and x['box']['l'] < desc_right and code.fullmatch(x['text'])
                           and any(aligned(x,n) and n['box']['l']>=amount['box']['l']-20
                                   and TAXED_AMOUNT.fullmatch(n['text']) for n in body)]
                # A single anchor needs the complete Qty/Price/Amount grid as corroboration.
                if kind == 'receipt' and (len(anchors) >= 2 or (len(anchors) == 1 and pheaders)):
                    for ai,anchor in enumerate(anchors):
                        end=anchors[ai+1]['box']['t'] if ai+1<len(anchors) else bottom
                        texts=[x for x in body if anchor['box']['b']-2<=x['box']['t']<end
                               and abs(x['box']['l']-desc['box']['l'])<=max(20,(desc['box']['b']-desc['box']['t'])*2)
                               and re.search(r'[A-Za-z]',x['text']) and not code.fullmatch(x['text'])
                               and not SUMMARY.fullmatch(x['text']) and not re.match(r'(?:qty|quantity)\s*:',x['text'],re.I)]
                        amounts=[x for x in body if aligned(anchor,x) and x['box']['l']>=amount['box']['l']-20 and TAXED_AMOUNT.fullmatch(x['text'])]
                        if not texts or not amounts:continue
                        q=[x for x in body if aligned(anchor,x) and abs(x['box']['l']-qheaders[0]['box']['l'])<max(15,qheaders[0]['box']['r']-qheaders[0]['box']['l']) and re.fullmatch(NUMBER,x['text'])]
                        p=[x for x in body if aligned(anchor,x) and re.fullmatch(NUMBER,x['text'])
                           and any(abs(x['box']['r']-h['box']['r'])<max(10,h['box']['r']-h['box']['l']) for h in pheaders)]
                        merged=[x for x in body if not q and not p and aligned(anchor,x) and QTY_PRICE_CELL.fullmatch(x['text'])
                                and x['box']['l']>=qheaders[0]['box']['l'] and x['box']['r']<amount['box']['l']]
                        if len(merged)==1:
                            # Both values are printed in one source line; keep that whole line as evidence.
                            parts=QTY_PRICE_CELL.fullmatch(merged[0]['text'])
                            q=[{**merged[0],'text':parts[1]}]; p=[{**merged[0],'text':parts[2]}]
                        joined={**texts[0],'text':'\n'.join(x['text'] for x in texts),'evidence':[x['ev'] for x in texts]}
                        result.append(row([joined],amounts,q,p))
                    continue
                descriptions = [x for x in body if desc['box']['l']-max(20,(desc['box']['b']-desc['box']['t'])*2) <= x['box']['l'] < desc_right and re.search(r'[A-Za-zÀ-ÿ]',x['text']) and x['text'] not in {'EUR','RM','MYR','IDR'} and not MULTIPLIER.fullmatch(x['text']) and not SUMMARY.fullmatch(x['text'])]
                grouped=[]
                for text in descriptions:
                    previous=grouped[-1][-1] if grouped else None
                    numeric_peer=any(aligned(text,x) and x['box']['l']>=desc_right and numeric(x['text']) for x in body)
                    if previous and not numeric_peer and abs(text['box']['l']-previous['box']['l'])<5 and -2<=text['box']['t']-previous['box']['b']<=(previous['box']['b']-previous['box']['t'])*.8:
                        grouped[-1].append(text)
                    else:grouped.append([text])
                descriptions=[{**group[0],'text':'\n'.join(x['text'] for x in group),'evidence':[x['ev'] for x in group]} for group in grouped]
                for di, text in enumerate(descriptions):
                    end = descriptions[di+1]['box']['t'] if di+1 < len(descriptions) else bottom
                    multipliers = [x for x in body if text['box']['b']-5 <= x['box']['t'] < end and MULTIPLIER.fullmatch(x['text'])]
                    matches = [x for x in body if x['box']['l'] >= amount['box']['l']-20 and (aligned(text,x) or (len(multipliers)==1 and aligned(multipliers[0],x))) and
                               (numeric(x['text']) if kind=='invoice' else re.fullmatch(r'(?:RM\s*|MYR\s*|EUR\s*)?' + NUMBER, x['text']))]
                    if kind=='invoice' and re.fullmatch(r'(?:net|gross)(?: worth| amount| total)?',amount['text'],re.I):
                        matches=[x for x in matches if abs(x['box']['r']-amount['box']['r'])<=max(20,(amount['box']['b']-amount['box']['t'])*1.5)]
                        for x in matches:
                            x['ev']={**x['ev'],'amount_basis':'net_excluding_tax' if amount['text'].lower().startswith('net') else 'gross_including_tax'}
                    if not matches and kind!='invoice':
                        continue
                    q = [x for x in body if qheaders and aligned(text,x) and abs(x['box']['l']-qheaders[0]['box']['l']) < max(30,qheaders[0]['box']['r']-qheaders[0]['box']['l']) and numeric(x['text'])]
                    p = [x for x in body if pheaders and aligned(text,x) and abs(x['box']['l']-pheaders[0]['box']['l']) < max(30,pheaders[0]['box']['r']-pheaders[0]['box']['l']) and numeric(x['text'])]
                    if kind=='invoice' and not matches and not (q or p):
                        continue  # A lone prose line is not a reliable partial row.
                    if len(multipliers) == 1 and not qheaders and not pheaders:
                        mult = multipliers[0]
                        match = MULTIPLIER.fullmatch(mult['text'])
                        # Both values are explicitly printed; retain the whole
                        # source-line box rather than inventing token boxes.
                        q, p = [{**mult, 'text': match[1]}], [{**mult, 'text': match[2]}]
                        for sub in q+p:
                            sub['ev'] = {**sub['ev'], 'text': mult['text'], 'bbox_scope': 'source_line'}
                    result.append(row([text], matches, q, p))
        elif kind == 'receipt':
            # Title-free row inference is not used to classify a document. For
            # a classified till slip, repeated right-hand monetary cells and a
            # nearby left description establish a row; barcodes stay excluded.
            width = document.get('pages', {}).get(str(page), {}).get('size', {}).get('width', 0)
            stop = min([x['box']['t'] for x in items if SUMMARY.fullmatch(x['text'])] or [float('inf')])
            amounts = [x for x in items if x['box']['t'] < stop and x['box']['l'] >= width*.55 and re.fullmatch(r'(?:RM\s*|MYR\s*)?-?\d+(?:[.,]\d+)*(?:\s+[A-Z])?',x['text'])]
            recovered = []
            used = set()
            sku_layout = sum(x['box']['l']<width*.5 and x['box']['t']<stop and bool(re.fullmatch(r'\d{8,}',x['text'])) for x in items)>=2
            for amount in amounts:
                h = amount['box']['b']-amount['box']['t']
                nearby = [x for x in items if x['box']['l'] < width*.5 and x['box']['r']<amount['box']['l'] and re.search(r'[A-Za-z]',x['text']) and not SUMMARY.fullmatch(x['text']) and x['ev']['source_ref'] not in used]
                if sku_layout:
                    nearby=[x for x in nearby if 0 <= amount['box']['t']-x['box']['t'] <= h*2.2]
                else:
                    nearby=[x for x in nearby if aligned(x,amount)]
                if not nearby:
                    continue
                text = max(nearby, key=lambda x:x['box']['t']) if sku_layout else min(nearby,key=lambda x:abs(center(x)-center(amount)))
                used.add(text['ev']['source_ref'])
                recovered.append(row([text],[amount]))
            prefixes=[re.fullmatch(r'(\d+(?:[.,]\d+)?)\s+([A-Za-z\[\(].*)',r['description']['value'] or '') for r in recovered]
            if not sku_layout and len(recovered)>=2 and all(prefixes):
                for item,match in zip(recovered,prefixes):
                    source=item['description']
                    item['description']={**source,'value':match[2],
                        'candidates':[{**candidate,'value':match[2]} for candidate in source['candidates']]}
                    value=number(match[1],currency,3)
                    item['quantity']=field(match[1],value,source['evidence'],'mapped',
                        [{'raw_text':match[1],'value':value,'evidence':source['evidence']}])
            if len(recovered) >= 2:
                result.extend(recovered)
    if result:
        issue('LAYOUT_LINE_RECOVERY', 'Rows recovered from source line geometry; missing cells were not inferred')
    return result


RECEIPT_MONEY = re.compile(r'^\(?(?:RM)?\s*(\d[\d,]*\.\d{2})\s*\*?\s*(?:[A-Za-z]{1,2})?$')
RECEIPT_END = re.compile(r'(?i)\b(?:total|sub-?\s*total|rounding|cash|tender|change|gst\s*summary)\b')
RECEIPT_HEAD = re.compile(r'(?i)\b(?:[qo]ty\.?|item|description|u/p|price|disc%?|amoun\w*|tax)\b')
AT_PRICE = re.compile(r'@\s*(\d+(?:[. ]\d{2,4})?)\s*$')


_STACKED_HEADER = re.compile(r'(?i)\b(?:u\.?\s*price|unit\s+price|price|u/p)\b')
_STACKED_AMOUNT = re.compile(r'(?i)^(?:amount|amt)$')
_STACKED_QUANTITY = re.compile(r'(\d+(?:\.\d+)?)(?:\s+[A-Za-z]{1,4})?$')
_STACKED_MONEY = re.compile(r'-?\d+\.\d{2}$')


def recover_stacked_rows(document, currency, location, field, issue):
    """POS rows that print the name on one line and "1.00 PC * 9.30 0.00 9.30" beneath it.

    Published only if, in exact Decimal, every row's quantity x price less its printed
    discount equals its amount, and the amounts add up to a printed summary value.
    """
    lines = []
    for item in records(document):
        prov = item.get('prov', [])
        text = item.get('text', '').strip()
        if len(prov) != 1 or prov[0].get('page_no') != 1 or not text:
            continue
        ev = location(document, 1, prov[0].get('bbox'), item.get('self_ref'), text)
        if ev.get('bbox'):
            ev.update(from_ocr=item.get('from_ocr'), ocr_score=item.get('ocr_score'), bbox_scope='source_line')
            lines.append({'text': text, 'ev': ev, 'box': ev['bbox'], 'mid': (ev['bbox']['t'] + ev['bbox']['b']) / 2})
    groups = []
    for line in sorted(lines, key=lambda x: x['mid']):
        height = line['box']['b'] - line['box']['t']
        if groups and abs(groups[-1][0]['mid'] - line['mid']) <= height * .65:
            groups[-1].append(line)
        else:
            groups.append([line])
    for group in groups:
        group.sort(key=lambda x: x['box']['l'])
    head = next((i for i, g in enumerate(groups) if any(_STACKED_HEADER.search(x['text']) for x in g)
                 and any(_STACKED_AMOUNT.match(x['text']) for x in g)), None)
    if head is None:
        return []
    rows, total, name, used = [], Decimal('0'), None, None
    for index in range(head + 1, len(groups)):
        group = groups[index]
        money = [x for x in group if _STACKED_MONEY.fullmatch(x['text'])]
        quantity = _STACKED_QUANTITY.fullmatch(group[0]['text'])
        if len(group) == 1 and not money and not quantity and re.search(r'[A-Za-z]{2}', group[0]['text']) and not SUMMARY.fullmatch(group[0]['text']):
            name = group[0]
            continue
        if quantity and len(money) >= 2 and group[0] not in money:
            if name is None or len(money) > 3:
                return []
            price, amount = Decimal(money[0]['text']), Decimal(money[-1]['text'])
            discount = Decimal(money[1]['text']) if len(money) == 3 else Decimal('0')
            count = Decimal(quantity[1])
            if count * price - discount != amount:
                return []
            cell = lambda raw, value, evidence: field(raw, value, evidence, 'mapped',
                                                      [{'raw_text': raw, 'value': value, 'evidence': evidence}])
            rows.append({'description': cell(name['text'], ' '.join(name['text'].split()), [name['ev']]),
                         'quantity': cell(group[0]['text'], number(quantity[1], currency, 3), [group[0]['ev']]),
                         # With a line discount, quantity x price is not the amount, so the price cell stays empty.
                         'unit_price': cell(money[0]['text'], number(money[0]['text'], currency, 4), [money[0]['ev']]) if not discount else field(),
                         'line_total': cell(money[-1]['text'], number(money[-1]['text'], currency), [money[-1]['ev']])})
            total += amount
            name, used = None, index
            continue
        if rows or name is not None:
            break
    if not rows or name is not None:
        return []
    printed = {Decimal(token) for g in groups[used + 1:] for x in g for token in re.findall(r'\d+\.\d{2}\b', x['text'])}
    if total not in printed:
        return []
    issue('LAYOUT_LINE_RECOVERY', 'Stacked rows recovered from source line geometry and checked in exact decimals against a printed total; missing cells were not inferred')
    return rows


def recover_receipt_rows(document, currency, location, field, issue):
    """Item rows from one receipt's OCR geometry, published only if their amounts add up to a printed summary value.

    Nothing is inferred. A printed unit price is kept only when quantity x price equals the
    printed amount, so a discount or tax-inclusive amount leaves that cell empty, not wrong.
    """
    lines = []
    for item in records(document):
        prov = item.get('prov', [])
        text = item.get('text', '').strip()
        if len(prov) != 1 or prov[0].get('page_no') != 1 or not text:
            continue
        ev = location(document, 1, prov[0].get('bbox'), item.get('self_ref'), text)
        if ev.get('bbox'):
            ev.update(from_ocr=item.get('from_ocr'), ocr_score=item.get('ocr_score'), bbox_scope='source_line')
            lines.append({'text': text, 'ev': ev, 'box': ev['bbox'], 'mid': (ev['bbox']['t'] + ev['bbox']['b']) / 2})
    groups = []
    for line in sorted(lines, key=lambda x: x['mid']):
        height = line['box']['b'] - line['box']['t']
        if groups and abs(groups[-1][0]['mid'] - line['mid']) <= height * .65:
            groups[-1].append(line)
        else:
            groups.append([line])
    for group in groups:
        group.sort(key=lambda x: x['box']['l'])
    # The item heading comes before the first amount. A later one belongs to the GST summary.
    first_money = next((i for i, g in enumerate(groups) if any(RECEIPT_MONEY.match(x['text']) for x in g)), len(groups))
    head = next((i for i, g in enumerate(groups[:first_money]) if len(RECEIPT_HEAD.findall(' '.join(x['text'] for x in g))) >= 2), None)
    first = head + 1 if head is not None else next((i for i, g in enumerate(groups) if RECEIPT_MONEY.match(g[-1]['text'])), None)
    if first is None:
        return []
    end = next((i for i in range(first, len(groups)) if RECEIPT_END.search(' '.join(x['text'] for x in groups[i]))), None)
    if end is None or end == first:
        return []
    region = groups[first:end]
    right = max(x['box']['r'] for g in region for x in g)
    width = right - min(x['box']['l'] for g in region for x in g)
    columns = {}
    if head is not None:
        for x in groups[head]:
            word = x['text'].casefold()
            if re.fullmatch(r'[qo]ty\.?', word):
                columns['quantity'] = x['box']
            elif word == 'u/p' or word.startswith('price'):
                columns['unit_price'] = x['box']
    leading_quantity = head is not None and bool(re.search(r'(?i)\b[qo]ty\b', ' '.join(x['text'] for x in groups[head])))

    def in_column(x, box):
        return box['l'] - 12 <= (x['box']['l'] + x['box']['r']) / 2 <= box['r'] + 12

    found, spare = [], []
    if head is None:
        # No column heading, and a tilted scan can shift text and amounts by a line: pair them in printed order.
        flat = sorted((x for g in region for x in g), key=lambda x: x['mid'])
        amounts = [x for x in flat if RECEIPT_MONEY.match(x['text']) and x['box']['r'] >= right - width * .2]
        texts = [x for x in flat if x not in amounts and re.search(r'[A-Za-z]{3}', x['text'])]
        if not texts or len(amounts) < len(texts):
            return []
        found = [{'amount': a, 'left': [t], 'more': []} for a, t in zip(amounts, texts)]
        spare = amounts[len(texts):]
    else:
        for group in region:
            last = group[-1]
            if RECEIPT_MONEY.match(last['text']) and last['box']['r'] >= right - width * .2:
                found.append({'amount': last, 'left': group[:-1], 'more': []})
            elif found:
                found[-1]['more'].extend(group)
    # On a tilted scan the printed total can sit just above its own label: a bare amount with no text is not an item.
    while found and not found[-1]['left'] and not found[-1]['more']:
        found.pop()
    rows, total = [], Decimal('0')
    for row in found:
        amount = Decimal(RECEIPT_MONEY.match(row['amount']['text'])[1].replace(',', ''))
        total += amount
        picked = {key: next((x for x in row['left'] if in_column(x, box) and re.fullmatch(r'\d+(?:\.\d+)?', x['text'])), None)
                  for key, box in columns.items()}
        words = [x for x in row['left'] + row['more'] if x not in picked.values()
                 and sum(c.isalnum() for c in x['text']) >= 3 and re.search(r'[A-Za-z]', x['text'])]
        if not words and not any(re.search(r'@', x['text']) for x in row['more']):
            return []
        joined = ' '.join(x['text'] for x in words + [x for x in row['more'] if x not in words and x not in picked.values()])
        price = picked.get('unit_price')['text'] if picked.get('unit_price') else None
        at = AT_PRICE.search(joined)
        if not price and at:
            price = at[1].replace(' ', '.')
        lead = re.match(r'^(\d{1,3})\s*(?=[A-Za-z])', words[0]['text']) if words and leading_quantity and 'quantity' not in columns else None
        quantity = picked['quantity']['text'] if picked.get('quantity') else (lead[1] if lead else None)
        names = [re.sub(r'\s*@.*$', '', re.sub(r'^-\s*', '', x['text'])) for x in words]
        if lead:
            names[0] = names[0][lead.end():]
        name = ' '.join(n.strip() for n in names if n.strip())
        if not name:
            return []
        description = field(' '.join(x['text'] for x in words), name, [x['ev'] for x in words], 'mapped',
                            [{'raw_text': ' '.join(x['text'] for x in words), 'value': name, 'evidence': [x['ev'] for x in words]}])
        cell = lambda raw, value, ev: field(raw, value, ev, 'mapped', [{'raw_text': raw, 'value': value, 'evidence': ev}])
        sources = [picked['quantity']['ev']] if picked.get('quantity') else [x['ev'] for x in words[:1]]
        quantity_cell, price_cell = field(), field()
        try:
            if quantity:
                quantity_cell = cell(quantity, number(quantity, currency, 3), sources)
            if quantity and price:
                value = number(price, currency, 4)
                if abs(Decimal(quantity) * Decimal(value) - amount) <= Decimal('0.01'):
                    evidence = [picked['unit_price']['ev']] if picked.get('unit_price') else [x['ev'] for x in row['left'] + row['more'] if '@' in x['text'] or AT_PRICE.search(x['text']) or x['text'] == price][:2] or sources
                    price_cell = cell(price, value, evidence)
        except (ValueError, ArithmeticError):
            # An unreadable number leaves its cell empty; the printed amount and description still stand.
            pass
        rows.append({'description': description, 'line_total': cell(row['amount']['text'], str(amount), [row['amount']['ev']]),
                     'quantity': quantity_cell, 'unit_price': price_cell})
    printed = {Decimal(token.replace(',', '')) for x in [x for g in groups[end:] for x in g] + spare
               for token in re.findall(r'\d[\d,]*\.\d{2}\b', x['text'])}
    if not rows or not any(abs(total - value) <= Decimal('0.01') for value in printed):
        return []
    issue('LAYOUT_LINE_RECOVERY', 'Receipt rows recovered from source line geometry and checked against a printed total; missing cells were not inferred')
    return rows


_ITEM_HEADER_WORD = re.compile(r'(?i)\b(?:desc(?:ription)?|item|particulars|qty|quantity|u\.?\s*price|unit\s+price|price|u/p|amount|amt)\b')


def item_structure(document, rows):
    """Whether the source prints item rows. A fact for validation: zero lines is right only for 'not_itemized'.

    Errs towards 'unmapped_rows', which sends the document to a person, not towards hiding rows.
    """
    if rows:
        return {'status': 'itemized', 'rows': len(rows)}
    if any(len(table.get('data', {}).get('table_cells', [])) >= 4 for table in document.get('tables', [])):
        return {'status': 'unmapped_rows', 'reason': 'A table is printed, and no rows were read from it.'}
    groups = defaultdict(list)
    for item in records(document):
        prov = item.get('prov') or []
        box = prov[0].get('bbox') if len(prov) == 1 else None
        if box and 't' in box and 'b' in box:
            groups[(prov[0].get('page_no'), round((box['t'] + box['b']) / 2 / max(abs(box['b'] - box['t']), 1)))].append(item.get('text', ''))
    for texts in groups.values():
        joined = ' '.join(texts)
        if len(set(word.casefold() for word in _ITEM_HEADER_WORD.findall(joined))) >= 2:
            return {'status': 'unmapped_rows', 'reason': 'An item heading is printed, and no rows were read beneath it.'}
    return {'status': 'not_itemized'}
