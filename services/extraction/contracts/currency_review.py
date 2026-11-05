"""Ranked currency proposals for a person to confirm, computed when a document is read.

A proposal is advice. It never fills the currency field and is never source evidence:
geography and a bare "$" do not prove a currency. No probability is attached, because
none has been calibrated. Rank comes from the kind of clue, then how many clues agree.
"""
import re

from services.extraction.contracts.accounting_contract import CURRENCY_MINOR_UNITS, effective_value

VERSION = 'currency-review/1'
PROPOSED = 'proposed_not_verified'
DOLLAR_FAMILY = tuple(code for code in ('USD', 'SGD', 'AUD', 'CAD', 'NZD', 'HKD', 'BND', 'TWD') if code in CURRENCY_MINOR_UNITS)

# Lower ranks first: printed on the document, then the verified supplier, then context.
TIER = {'printed_code': 0, 'supplier_records': 1, 'supplier_master_address': 2, 'supplier_address': 2,
        'customer_address': 3, 'supplier_company_form': 3, 'tax_id_format': 3, 'customer_company_form': 4}
SUPPORT = {0: 'printed_on_document', 1: 'supplier_records', 2: 'document_context', 3: 'document_context', 4: 'document_context'}
CLUE_NAME = {'printed_code': 'Printed on the document', 'supplier_records': "This supplier's approved records",
             'supplier_master_address': "Address in this supplier's contact record", 'supplier_address': 'Supplier address on the document',
             'customer_address': 'Customer address on the document', 'supplier_company_form': 'Supplier company form',
             'customer_company_form': 'Customer company form', 'tax_id_format': 'Tax number format'}

COUNTRIES = (('malaysia', 'MYR'), ('singapore', 'SGD'), ('india', 'INR'), ('indonesia', 'IDR'), ('ireland', 'EUR'), ('germany', 'EUR'),
             ('france', 'EUR'), ('spain', 'EUR'), ('italy', 'EUR'), ('netherlands', 'EUR'), ('united kingdom', 'GBP'), ('england', 'GBP'),
             ('scotland', 'GBP'), ('wales', 'GBP'), ('united states', 'USD'), ('u\\.s\\.a\\.?', 'USD'), ('usa', 'USD'), ('australia', 'AUD'),
             ('new zealand', 'NZD'), ('canada', 'CAD'), ('hong kong', 'HKD'), ('japan', 'JPY'), ('thailand', 'THB'), ('philippines', 'PHP'))
_US_STATE = ('AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY')
_MY_STATE = 'selangor|johor|kuala lumpur|pulau pinang|penang|perak|kedah|melaka|malacca|sabah|sarawak|negeri sembilan|pahang|terengganu|kelantan|perlis|putrajaya|labuan'
_IN_STATE = 'maharashtra|delhi|karnataka|tamil nadu|gujarat|west bengal|uttar pradesh|madhya pradesh|rajasthan|telangana|kerala|punjab|haryana|andhra pradesh|bihar|odisha'
PATTERNS = (
    (re.compile(rf'\b(?:{_US_STATE}),?\s+\d{{5}}(?:-\d{{4}})?\b'), 'USD', 'US state and ZIP code'),
    (re.compile(r'\b[ADF]PO\s+A[AEP]\s+\d{5}\b'), 'USD', 'US military postal address'),
    (re.compile(rf'\b\d{{5}}\b[^\n]*\b(?:{_MY_STATE})\b|\b(?:{_MY_STATE})\b[^\n]*\b\d{{5}}\b', re.I | re.S), 'MYR', 'Malaysian postcode and state'),
    (re.compile(rf'\b(?:{_IN_STATE})\b[^\n]*\b\d{{6}}\b', re.I), 'INR', 'Indian state and PIN code'),
    (re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b'), 'GBP', 'UK postcode'),
    (re.compile(r'\b(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d\b'), 'CAD', 'Canadian province and postal code'),
    (re.compile(r'\b(?:NSW|VIC|QLD|TAS|ACT)\s+\d{4}\b'), 'AUD', 'Australian state and postcode'),
    (re.compile(r'\bDublin\s+\d{1,2}\b'), 'EUR', 'Dublin postal district'),
)
COMPANY_FORMS = ((re.compile(r'\b(?:sdn\.?\s*bhd|berhad|s/b)\b', re.I), 'MYR', 'Sdn Bhd'), (re.compile(r'\bpvt\.?\s*ltd\b', re.I), 'INR', 'Pvt Ltd'),
                 (re.compile(r'\bpte\.?\s*ltd\b', re.I), 'SGD', 'Pte Ltd'), (re.compile(r'\bpty\.?\s*ltd\b', re.I), 'AUD', 'Pty Ltd'),
                 (re.compile(r'\bgmbh\b', re.I), 'EUR', 'GmbH'), (re.compile(r'\b(?:llc|inc\.?)\s*$', re.I), 'USD', 'LLC or Inc'))
GSTIN = re.compile(r'\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b')
ABN = re.compile(r'\bABN\b\s*:?\s*\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b')


def location_of(text):
    """Every currency an address points to, with the printed fragment that says so."""
    text = text or ''
    found = []
    for name, code in COUNTRIES:
        match = re.search(rf'\b{name}(?![a-z])', text, re.I)
        if match:
            found.append((code, match.group(0), 'country name'))
    for pattern, code, why in PATTERNS:
        match = pattern.search(text)
        if match:
            found.append((code, ' '.join(match.group(0).split()), why))
    return found


def _first_evidence(row):
    for item in (row or {}).get('evidence') or []:
        if isinstance(item, dict) and item.get('page'):
            return {'page': item.get('page'), 'bbox': item.get('bbox')}
    return {'page': None, 'bbox': None}


def _value(row):
    if not row:
        return None
    try:
        return effective_value(row)
    except ValueError:
        return None


def build_currency_review(fields, *, supplier_record=None):
    """fields: stored field rows. supplier_record: only a supplier found in this tenant's contacts,
    {'name','address','records':[{'currency','count'}]}. Returns the review, or None with no currency field."""
    rows = {row['field_key']: row for row in fields}
    currency = rows.get('currency')
    if currency is None:
        return None
    candidates = [c for c in currency.get('candidates') or [] if isinstance(c, dict) and c.get('source') != 'ai']
    printed_symbol = next((c for c in candidates if c.get('value') is None and '$' in str(c.get('raw_text') or '')), None)
    clues = []
    def clue(kind, code, text, why, row=None):
        if code in CURRENCY_MINOR_UNITS:
            clues.append({'kind': kind, 'name': CLUE_NAME[kind], 'currency': code, 'text': text, 'why': why, **_first_evidence(row)})
    for item in candidates:
        if item.get('value') and not currency.get('corrected_at'):
            clue('printed_code', item['value'], str(item.get('raw_text') or item['value']), 'A currency is printed but was not selected', {'evidence': item.get('evidence')})
    if supplier_record:
        for entry in supplier_record.get('records') or []:
            clue('supplier_records', entry['currency'], f"{supplier_record['name']}: {entry['count']} approved", 'Currency on approved records for this supplier')
        for code, text, why in location_of(supplier_record.get('address')):
            clue('supplier_master_address', code, text, why)
    for key, kind in (('supplier_address', 'supplier_address'), ('merchant_address', 'supplier_address'), ('provider_address', 'supplier_address'), ('customer_address', 'customer_address')):
        for code, text, why in location_of(_value(rows.get(key))):
            clue(kind, code, text, why, rows.get(key))
    for key, kind in (('supplier', 'supplier_company_form'), ('customer', 'customer_company_form')):
        name = _value(rows.get(key)) or ''
        for pattern, code, label in COMPANY_FORMS:
            if pattern.search(name):
                clue(kind, code, name, f'Company form {label}', rows.get(key))
    for key in ('supplier_tax_id', 'customer_tax_id'):
        tax = str(_value(rows.get(key)) or '')
        if GSTIN.search(tax):
            clue('tax_id_format', 'INR', tax, 'Indian GSTIN format', rows.get(key))
        elif ABN.search(tax):
            clue('tax_id_format', 'AUD', tax, 'Australian ABN format', rows.get(key))
    # A "$" limits the choice. A place whose currency is not a dollar cannot propose it.
    conflicts, usable = [], []
    for item in clues:
        if printed_symbol and item['kind'] != 'printed_code' and item['currency'] not in DOLLAR_FAMILY:
            conflicts.append(item)
        else:
            usable.append(item)
    grouped = {}
    for item in usable:
        grouped.setdefault(item['currency'], []).append(item)
    order = sorted(grouped, key=lambda code: (min(TIER[c['kind']] for c in grouped[code]), -len({c['kind'] for c in grouped[code]}), code))
    proposals = [{'rank': index + 1, 'currency': code, 'status': PROPOSED,
                  'support': SUPPORT[min(TIER[c['kind']] for c in grouped[code])], 'clues': grouped[code]} for index, code in enumerate(order)]
    alternatives = [code for code in DOLLAR_FAMILY if code not in grouped] if printed_symbol else []
    return {'version': VERSION,
            'needed': _value(currency) is None,
            'printedSymbol': {'symbol': '$', 'text': printed_symbol.get('raw_text'), **_first_evidence({'evidence': printed_symbol.get('evidence')})} if printed_symbol else None,
            'proposals': proposals, 'alternatives': alternatives,
            'conflicts': [{'currency': c['currency'], 'name': c['name'], 'text': c['text']} for c in conflicts],
            'disagreement': len(proposals) > 1,
            'sourceVerified': False,
            'statement': 'A suggestion to check, not evidence. The document does not print this currency unless a clue says so.'}


def decision_details(review, value, previous):
    """What the reviewer was shown and chose, for the audit log."""
    chosen = next((p for p in (review or {}).get('proposals') or [] if p['currency'] == value), None)
    return {'field': 'currency', 'value': value, 'previous': previous, 'version': VERSION,
            'selectedRank': chosen['rank'] if chosen else None,
            'selectedBasis': [{'kind': c['kind'], 'text': c['text']} for c in chosen['clues']] if chosen else [],
            'proposalsShown': [p['currency'] for p in (review or {}).get('proposals') or []],
            'printedSymbol': ((review or {}).get('printedSymbol') or {}).get('text'),
            'sourceVerified': False, 'basis': 'accountant_decision'}
