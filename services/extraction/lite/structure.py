"""OCR rows -> accounting fields with a text model, every value checked locally.

The model only decides WHICH printed text belongs to which field. A value is
used only when this module finds it printed in the local OCR rows:

    printed and normalised locally  -> filled in for the reviewer (AI_PREFILL=verified)
    printed, normalisation unsure   -> shown as an AI candidate, field stays empty
    not printed in the source       -> discarded (only a short hash is recorded)

Amounts, dates and currency codes are normalised by this module, not trusted
from the model. Nothing is calculated or inferred, and the document still
requires human review and approval.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from services.extraction.ai import text_llm
from services.extraction.contracts.accounting_contract import CURRENCY_MINOR_UNITS
from services.extraction.lite.pages import layout_text
from services.extraction.lite.privacy import Masker

VERSION = 'lite-structure/0.1'
KINDS = ('invoice', 'bill', 'receipt', 'credit_note', 'debit_note')
MAX_PROMPT_CHARACTERS = 24_000
MAX_LINE_ITEMS = 200

# Neutral names the model fills in -> the source key each document type stores.
# Seller and buyer identity printed on invoices and on credit/debit notes.
_PARTIES = {'seller_name': 'supplier_name', 'seller_address': 'supplier_address',
            'seller_registration_id': 'supplier_registration_id', 'seller_sst_id': 'supplier_sst_id',
            'seller_tin': 'supplier_tin', 'seller_tax_id': 'supplier_tax_id',
            'buyer_name': 'buyer_name', 'buyer_address': 'customer_address', 'buyer_tax_id': 'customer_tax_id'}
_NOTE = {**_PARTIES, 'document_date': 'invoice_date', 'original_invoice_number': 'original_invoice_number',
         'note_reason': 'note_reason', 'currency': 'currency', 'subtotal': 'subtotal',
         'tax_amount': 'tax_amount', 'total_amount': 'total_amount'}
PROFILE_FIELDS = {
    'invoice': {**_PARTIES,
                'document_number': 'invoice_number', 'document_date': 'invoice_date', 'due_date': 'due_date',
                'purchase_order_number': 'purchase_order_number', 'payment_terms': 'payment_terms',
                'currency': 'currency', 'subtotal': 'subtotal', 'discount': 'discount',
                'tax_amount': 'tax_amount', 'total_amount': 'total_amount', 'amount_due': 'amount_due'},
    'receipt': {'seller_name': 'merchant', 'seller_address': 'merchant_address',
                'document_number': 'receipt_number', 'document_date': 'transaction_date',
                'transaction_time': 'transaction_time', 'payment_method': 'payment_method',
                'currency': 'currency', 'subtotal': 'subtotal', 'discount': 'discount',
                'tax_amount': 'tax_amount', 'rounding': 'rounding', 'total_amount': 'total_amount'},
    'bill': {'seller_name': 'issuer', 'seller_address': 'provider_address', 'buyer_name': 'buyer_name',
             'account_number': 'account_number',
             # The printed bill number; standardisation already prefers bill_number for the reference.
             'document_number': 'bill_number', 'document_date': 'bill_date', 'due_date': 'due_date',
             'billing_period': 'billing_period', 'currency': 'currency', 'subtotal': 'subtotal',
             'tax_amount': 'tax_amount', 'total_amount': 'total_amount', 'amount_due': 'amount_due'},
    'credit_note': {**_NOTE, 'document_number': 'credit_note_number'},
    'debit_note': {**_NOTE, 'document_number': 'debit_note_number'},
}
MODEL_FIELDS = {
    'seller_name': 'business that issued the document (supplier, merchant or service provider)',
    'seller_address': 'address of that business',
    'seller_registration_id': 'its company registration number (SSM / ROC / BRN, "No. Syarikat", "Co. Reg. No.")',
    'seller_sst_id': 'its SST registration number',
    'seller_tin': 'its TIN (tax identification number)',
    'seller_tax_id': 'its tax number when printed only as "Tax Id", GST or VAT number (not TIN or SST)',
    'buyer_name': 'customer the document is addressed to',
    'buyer_address': 'address of that customer',
    'buyer_tax_id': 'the customer\'s tax number, when printed',
    'note_reason': 'reason printed on a credit or debit note',
    'rounding': 'cash rounding adjustment printed on a receipt (may be negative)',
    'document_number': 'invoice / receipt / bill / note number: the number itself with any letter prefix such as DN- or B, without its label',
    'document_date': 'issue or transaction date',
    'due_date': 'payment due date ("Due Date", "Pay Before", "Tarikh Akhir Bayaran")',
    'transaction_time': 'time of sale on a receipt (HH:MM)',
    'purchase_order_number': 'purchase order reference',
    'payment_terms': 'payment terms as printed, e.g. Net 30',
    'payment_method': 'one of cash, card, bank_transfer, cheque, e_wallet, direct_debit, other',
    'account_number': 'customer account number on a utility or service bill ("Account No." / "No. Akaun"); never the bill number',
    'billing_period': 'billing period as printed on a bill',
    'original_invoice_number': 'invoice a credit or debit note refers to',
    'currency': 'ISO 4217 code (RM means MYR)',
    'subtotal': 'amount before tax (on a bill: "Current Charges" / "Jumlah Caj Semasa")',
    'discount': 'document-level discount amount',
    'tax_amount': 'total tax (SST, GST, VAT, service tax)',
    'total_amount': 'final total including tax',
    'amount_due': 'amount still payable, when printed separately from the total (on a bill: "Amount Due" / "Jumlah Perlu Dibayar", which can include arrears)',
}
DATE_FIELDS = {'document_date', 'due_date'}
NAME_FIELDS = {'seller_name', 'buyer_name'}
REFERENCE_FIELDS = {'document_number', 'account_number', 'original_invoice_number', 'purchase_order_number'}
# A printed label the model copied in front of a reference number ("Bill No.: B293891844").
_REFERENCE_LABEL = re.compile(r'^[^\d:#]{0,40}\b(?:no|number|nombor|num|ref|reference|rujukan)\b\.?\s*[:#]\s*(?=\S)', re.I)
# A document heading printed on the same line as a name or address ("... Berhad | BIL / BILL WATER").
_HEADING_COLUMN = re.compile(
    r'(?:(?:bil|bill|invoice|invois|tax|cukai|receipt|resit|nota|note|credit|kredit|debit|statement|penyata|'
    r'water|air|electricity|elektrik|internet|broadband|telefon|telephone|utility|utiliti)\b[\s/:.-]*)+', re.I)


def drop_heading_columns(value, *, name=False):
    """Remove document-heading columns joined to a name or address by " | ".

    The layout joins text printed far apart on one line with " | "; a heading such
    as "BIL / BILL WATER" beside the provider name is not part of the name. A name
    keeps only its first remaining column. Nothing else is changed.
    """
    if not value or ' | ' not in value:
        return value
    lines = []
    for line in value.split('\n'):
        parts = [part.strip() for part in line.split(' | ')]
        kept = [part for part in parts if part and not _HEADING_COLUMN.fullmatch(part)]
        if name and kept:
            kept = kept[:1]
        if kept:
            lines.append(' '.join(kept))
    return '\n'.join(lines) or value
ADDRESS_FIELDS = {'seller_address', 'buyer_address'}
_DATE_TOKEN = r'\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})'
_PERIOD = re.compile(rf'({_DATE_TOKEN})\s*(?:-|–|to|hingga|until|sehingga)\s*({_DATE_TOKEN})', re.I)
MONEY_FIELDS = {'subtotal', 'discount', 'tax_amount', 'total_amount', 'amount_due', 'rounding'}
PAYMENT_METHODS = {'cash', 'card', 'bank_transfer', 'cheque', 'e_wallet', 'direct_debit', 'other'}
CURRENCY_ALIASES = {'RM': 'MYR', 'S$': 'SGD', 'US$': 'USD', 'A$': 'AUD', 'HK$': 'HKD', '€': 'EUR', '£': 'GBP'}
MONTHS = {name: number for number, names in enumerate([
    ('jan', 'january', 'januari'), ('feb', 'february', 'februari'), ('mar', 'march', 'mac'),
    ('apr', 'april'), ('may', 'mei'), ('jun', 'june'), ('jul', 'july', 'julai'),
    ('aug', 'august', 'ogos', 'ogs'), ('sep', 'sept', 'september'), ('oct', 'october', 'okt', 'oktober'),
    ('nov', 'november'), ('dec', 'december', 'dis', 'disember')], 1) for name in names}

SYSTEM_PROMPT = """You read the OCR text of ONE business document and return its accounting fields as JSON.
The document is usually Malaysian (currency RM/MYR, SST, dates normally day/month/year) but may be from anywhere.

Rules:
- Use only what is printed in the text. If something is not printed, use null. Never guess, calculate, translate or infer.
- Every field is an object {"value": ..., "raw": ...}. "raw" is the text exactly as printed, copied character for character. "value" is the normalised form.
- Dates: value is YYYY-MM-DD. Amounts: digits with a dot and two decimals, no currency symbol and no thousands separators. Currency: an ISO 4217 code.
- Tokens such as [[ACCOUNT_1]] or [[PHONE_1]] hide private values. If a field's value is such a token, return the token unchanged.
- document_type is one of: invoice, bill, receipt, credit_note, debit_note, unknown. A supplier's (tax) invoice is "invoice". A utility, telecom or other recurring service bill with a customer account number is "bill". A till, cash-sale or payment receipt is "receipt".
- line_items: one entry per printed goods or service row. Do not include subtotal, tax, rounding, total or payment rows. Use null for a column that is not printed.
- Text columns separated by " | " were printed far apart on the same line.
Return one JSON object and nothing else."""


def _user_prompt(text):
    field_lines = '\n'.join(f'  "{name}": {description}' for name, description in MODEL_FIELDS.items())
    return ('Return JSON with exactly these keys:\n'
            '{"document_type": "...",\n'
            ' "fields": {<field name>: {"value": string or null, "raw": string or null}, ...},\n'
            ' "line_items": [{"description": string or null, "quantity": string or null, '
            '"unit_price": string or null, "line_total": string or null}]}\n\n'
            'Field names and meanings:\n' + field_lines + '\n\nDocument text:\n"""\n' + text + '\n"""')


def _compact(text):
    return re.sub(r'[^0-9a-z]+', '', str(text).casefold())


def _hash(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:12]


# "6 204,19" / "1.234,56" / "564,02": a comma followed by exactly two final digits is a
# decimal comma (common outside Malaysia). "1,234.56" and "1,234" keep their usual meaning.
_DECIMAL_COMMA = re.compile(r'(?<![\d.,])(?:\d{1,3}(?:[ .  ]\d{3})+|\d+),\d{2}(?![\d.,])')
# "6 204.19": thousands grouped with spaces before a decimal point.
_SPACED_THOUSANDS = re.compile(r'(?<![\d.,])\d{1,3}(?:[   ]\d{3})+\.\d+(?![\d.,])')


def _plain_number(token):
    """A printed amount token as a plain decimal string."""
    if _DECIMAL_COMMA.fullmatch(token):
        return re.sub(r'[ .  ]', '', token).replace(',', '.')
    return re.sub(r'[\s  ]', '', token).replace(',', '')


def _numbers(text):
    """Decimal values printed in a piece of text (thousands separators removed)."""
    found = []

    def take(match):
        try:
            found.append(Decimal(_plain_number(match.group(0))))
        except InvalidOperation:
            pass
        return ' '
    text = _SPACED_THOUSANDS.sub(take, _DECIMAL_COMMA.sub(take, text))
    for token in re.findall(r'(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])|(?<![\d.,])\d+(?:\.\d+)?(?![\d,])', text):
        try:
            found.append(Decimal(token.replace(',', '')))
        except InvalidOperation:
            continue
    return found


class SourceIndex:
    """Looks values up in the local OCR rows and returns evidence for the reviewer."""

    def __init__(self, document, line_rows):
        self.rows = document['rows']
        self.lines = [rows for rows in line_rows if rows]
        self.page_sizes = {page['page']: page['size'] for page in document['pages']}
        self._compact = [_compact(row['text']) for row in self.rows]

    def evidence(self, row):
        xs, ys = zip(*row['box_polygon_pixels'])
        return {'source_ref': f"#/rows/{row['order']}", 'page': row['page'], 'text': row['text'],
                'region_order': row['order'], 'bbox_original': [list(p) for p in row['box_polygon_pixels']],
                'bbox': {'l': min(xs), 't': min(ys), 'r': max(xs), 'b': max(ys), 'coord_origin': 'TOPLEFT'},
                'page_size': dict(self.page_sizes[row['page']]), 'coordinate_units': 'pixels',
                'engine_score': row['engine_score'], 'from_ocr': bool(row.get('from_ocr'))}

    def find_text(self, text, *, _depth=0):
        target = _compact(text)
        if len(target) < 2:
            return []
        hits = [row for row, compact in zip(self.rows, self._compact) if target in compact]
        if hits:
            return sorted(hits, key=lambda row: len(row['text']))[:2]
        for line in self.lines:
            if target in _compact(' '.join(row['text'] for row in line)):
                inside = [row for row in line if _compact(row['text']) and _compact(row['text']) in target]
                return inside or list(line)
        if _depth == 0:
            # A value printed over several lines, such as an address.
            parts = [part for part in re.split(r'[\n,;]+', str(text)) if len(_compact(part)) >= 3]
            if len(parts) > 1:
                found = [self.find_text(part, _depth=1) for part in parts]
                if all(found):
                    unique = {row['order']: row for rows in found for row in rows}
                    return [unique[order] for order in sorted(unique)]
        return []

    def _row_containing(self, text):
        """The shortest single source row that contains all of `text`, or None."""
        target = _compact(text)
        hits = [row for row, compact in zip(self.rows, self._compact) if target and target in compact]
        return min(hits, key=lambda row: len(row['text'])) if hits else None

    def address_lines(self, text):
        """An address laid out as printed: parts on one source row joined by ", ", rows by newlines.

        Returns (value, rows) when every part is printed and the address spans more than one
        row, else None so the caller keeps the single-line form.
        """
        parts = [part.strip() for part in re.split(r'[\n,;]+', str(text or '')) if len(_compact(part)) >= 2]
        if len(parts) < 2:
            return None
        lines, rows = [], []
        index = 0
        while index < len(parts):
            # Longest run of consecutive parts printed together on one row ("No. 2, Jalan Meranti").
            row, end = None, index
            for last in range(len(parts) - 1, index, -1):
                row = self._row_containing(' '.join(parts[index:last + 1]))
                if row is not None:
                    end = last
                    break
            if row is None:
                found = self.find_text(parts[index], _depth=1)
                if not found:
                    return None
                row = found[0]
            group = parts[index:end + 1]
            if rows and rows[-1] is row:
                lines[-1].extend(group)
            else:
                rows.append(row)
                lines.append(list(group))
            index = end + 1
        if len(lines) < 2:
            return None
        return '\n'.join(', '.join(line) for line in lines), rows

    def find_amount(self, amount):
        return [row for row in self.rows if amount in _numbers(row['text'])][:3]

    def find_token(self, pattern):
        return [row for row in self.rows if re.search(pattern, row['text'])][:2]


def _text(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.replace('\x00', '').strip()
    return value or None


def parse_amount(value, scale=2):
    """Decimal string with at most `scale` decimals, or None. Never rounds."""
    text = _text(value)
    if text is None:
        return None
    negative = text.startswith('-') or (text.startswith('(') and text.endswith(')'))
    core = re.sub(r'(?i)[a-z$€£¥()]', '', text).strip().lstrip('+-').strip()
    cleaned = _plain_number(core)
    if not re.fullmatch(r'\d+(?:\.\d+)?', cleaned):
        return None
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return None
    quantum = Decimal(1).scaleb(-scale)
    if number != number.quantize(quantum) or abs(number) >= Decimal(10) ** (14 - scale):
        return None
    if scale == 2:
        number = number.quantize(quantum)
    return format(-number if negative else number, 'f')


def parse_date(raw, order='DMY'):
    """ISO date read from printed text, or None. `order` settles 03/04/2026."""
    text = _text(raw)
    if text is None:
        return None, False

    def build(year, month, day):
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    match = re.search(r'(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', text)
    if match:
        return build(int(match[1]), int(match[2]), int(match[3])), False
    match = re.search(r'(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)', text)
    if match:
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        ambiguous = first <= 12 and second <= 12 and first != second
        if first > 12 >= second:
            return build(year, second, first), False
        if second > 12 >= first:
            return build(year, first, second), False
        day, month = (first, second) if order == 'DMY' else (second, first)
        return build(year, month, day), ambiguous
    match = re.search(r'(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[-/.\s]*([A-Za-z]{3,9})\.?,?[-/.\s]*(\d{4}|\d{2})(?!\d)', text)
    if match and match[2].casefold() in MONTHS:
        return build(int(match[3]), MONTHS[match[2].casefold()], int(match[1])), False
    match = re.search(r'([A-Za-z]{3,9})\.?\s*(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})(?!\d)', text)
    if match and match[1].casefold() in MONTHS:
        return build(int(match[3]), MONTHS[match[1].casefold()], int(match[2])), False
    return None, False


def parse_time(raw):
    text = _text(raw)
    if text is None:
        return None
    match = re.search(r'(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp])?\.?[Mm]?', text)
    if not match:
        return None
    hour, minute, second = int(match[1]), int(match[2]), match[3]
    if match[4]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match[4].lower() == 'p' else 0)
    if hour > 23 or minute > 59 or (second and int(second) > 59):
        return None
    return f'{hour:02d}:{minute:02d}' + (f':{second}' if second else '')


def _cell(state, value, *, method, raw=None, evidence=(), candidates=(), model=None, provider=None):
    return {'value': value, 'state': state, 'method': method, 'review_required': True,
            'raw_text': raw, 'evidence': list(evidence), 'candidates': list(candidates), 'confidence': None,
            'model_name': model, 'model_version': (provider + '/' + VERSION) if provider else VERSION}


def _read_method(rows):
    return 'OCR' if any(row.get('from_ocr') for row in rows) else 'PARSER'


class _Resolver:
    """Checks one model answer against the source and builds contract cells."""

    def __init__(self, index, masker, record, *, prefill, date_order, default_method):
        self.index, self.masker, self.prefill = index, masker, prefill
        self.date_order, self.default_method = date_order, default_method
        self.model, self.provider = record.get('model'), record.get('provider')
        self.issues = []
        self.outcomes = {'filled': 0, 'candidate': 0, 'discarded': 0, 'missing': 0}

    def _missing(self):
        self.outcomes['missing'] += 1
        return _cell('missing', None, method=self.default_method)

    def _discard(self, name, value, reason):
        self.outcomes['discarded'] += 1
        self.issues.append({'code': 'AI_VALUE_DISCARDED', 'field': name, 'reason': reason,
                            'value_hash': _hash(value)})
        return _cell('missing', None, method=self.default_method)

    def _accept(self, value, raw, rows, *, verified=True, note=None):
        evidence = [self.index.evidence(row) for row in rows]
        details = {'source': 'ai', 'value': value, 'auto_accepted': False,
                   'source_check': 'printed_in_source' if verified else note or 'printed_unverified_normalisation',
                   'model': self.model, 'provider': self.provider}
        if verified and self.prefill:
            self.outcomes['filled'] += 1
            return _cell('resolved', value, method=_read_method(rows), raw=raw, evidence=evidence,
                         candidates=[details], model=self.model, provider=self.provider)
        # Shown to the reviewer as a suggestion; the field itself stays empty.
        self.outcomes['candidate'] += 1
        cell = _cell('resolved', value, method='VLM', raw=raw, evidence=evidence,
                     model=self.model, provider=self.provider)
        cell['source_check'] = details['source_check']
        return cell

    def field(self, name, answer):
        answer = answer if isinstance(answer, dict) else {'value': answer, 'raw': answer}
        value = _text(self.masker.unmask(_text(answer.get('value'))))
        raw = _text(self.masker.unmask(_text(answer.get('raw')))) or value
        if value is None and raw is None:
            return self._missing()
        value = value if value is not None else raw
        if value.casefold() in {'null', 'none', 'n/a', 'na', '-', 'unknown', 'not printed', 'not available'}:
            return self._missing()
        if name in REFERENCE_FIELDS:
            value, raw = _REFERENCE_LABEL.sub('', value), _REFERENCE_LABEL.sub('', raw) if raw else raw
        if name in NAME_FIELDS or name in ADDRESS_FIELDS:
            is_name = name in NAME_FIELDS
            value, raw = drop_heading_columns(value, name=is_name), drop_heading_columns(raw, name=is_name)
        if name in MONEY_FIELDS:
            amount = parse_amount(raw) or parse_amount(value)
            if amount is None:
                return self._discard(name, value, 'not_a_decimal_amount')
            rows = self.index.find_amount(Decimal(amount).copy_abs())
            if not rows:
                return self._discard(name, value, 'not_found_in_source')
            return self._accept(amount, raw, rows)
        if name in DATE_FIELDS:
            rows = self.index.find_text(raw)
            if not rows:
                return self._discard(name, value, 'not_found_in_source')
            parsed, ambiguous = parse_date(raw, self.date_order)
            if parsed is None:
                # Printed, but this module cannot confirm the model's reading of it.
                model_date, _ = parse_date(value, 'DMY') if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else (None, False)
                if model_date is None:
                    return self._discard(name, value, 'date_not_readable')
                return self._accept(model_date, raw, rows, verified=False, note='date_format_not_verifiable')
            if ambiguous:
                self.issues.append({'code': 'DATE_ORDER_ASSUMED', 'field': name,
                                    'detail': 'Day and month are both 12 or less; read as '
                                              + ('day/month' if self.date_order == 'DMY' else 'month/day') + '.'})
            return self._accept(parsed, raw, rows)
        if name == 'currency':
            code = CURRENCY_ALIASES.get(value.upper(), value.upper())
            if code not in CURRENCY_MINOR_UNITS:
                code = CURRENCY_ALIASES.get((raw or '').upper().strip(), code)
            if code not in CURRENCY_MINOR_UNITS:
                return self._discard(name, value, 'unsupported_currency')
            printed = [code] + [alias for alias, target in CURRENCY_ALIASES.items() if target == code]
            rows = []
            for token in printed:
                rows = self.index.find_token(r'(?<![A-Za-z])' + re.escape(token) + r'(?![A-Za-z])')
                if rows:
                    break
            if not rows:
                return self._discard(name, value, 'not_found_in_source')
            return self._accept(code, rows[0]['text'], rows)
        if name == 'transaction_time':
            rows = self.index.find_text(raw)
            parsed = parse_time(raw) or parse_time(value)
            if not rows or parsed is None:
                return self._discard(name, value, 'not_found_in_source' if not rows else 'time_not_readable')
            return self._accept(parsed, raw, rows)
        if name == 'payment_method':
            method = value.casefold().replace(' ', '_').replace('-', '_')
            rows = self.index.find_text(raw)
            if method not in PAYMENT_METHODS or not rows:
                return self._discard(name, value, 'not_found_in_source' if method in PAYMENT_METHODS else 'unsupported_payment_method')
            return self._accept(method, raw, rows)
        if name == 'billing_period':
            # OCR often drops the spaces around the dash: keep both printed dates, normalise the joint.
            period = _PERIOD.search(raw or '') or _PERIOD.search(value)  # the printed separators win
            rows = self.index.find_text(value) or self.index.find_text(raw)
            if period and rows:
                return self._accept(f'{period[1]} - {period[2]}', raw, rows)
        if name in ADDRESS_FIELDS:
            # Keep the document's own line breaks, as printed and as the reviewer sees them.
            shaped = self.index.address_lines(value) or self.index.address_lines(raw)
            if shaped:
                return self._accept(shaped[0], raw, shaped[1])
        # Text: prefer the model's value when it is printed as given, else the printed raw text.
        rows = self.index.find_text(value)
        if rows:
            return self._accept(' '.join(value.split()) if '\n' not in value else ', '.join(
                part.strip() for part in value.splitlines() if part.strip()), raw, rows)
        rows = self.index.find_text(raw)
        if rows:
            printed = ', '.join(part.strip() for part in raw.splitlines() if part.strip())
            return self._accept(printed, raw, rows)
        return self._discard(name, value, 'not_found_in_source')

    def line(self, item):
        if not isinstance(item, dict):
            return None
        cells, supported = {}, 0
        description = _text(self.masker.unmask(_text(item.get('description'))))
        rows = self.index.find_text(description) if description else []
        if description and rows:
            supported += 1
            cells['description'] = self._line_cell(' '.join(description.split()), description, rows)
        else:
            cells['description'] = _cell('missing', None, method=self.default_method)
        for name, scale in (('quantity', 3), ('unit_price', 4), ('line_total', 2)):
            raw = _text(item.get(name))
            amount = parse_amount(raw, scale) if raw else None
            rows = self.index.find_amount(Decimal(amount).copy_abs()) if amount is not None else []
            if amount is not None and rows:
                supported += name != 'quantity'  # A bare quantity matches too many printed digits.
                cells[name] = self._line_cell(amount, raw, rows)
            else:
                cells[name] = _cell('missing', None, method=self.default_method)
        return cells if supported else None

    def _line_cell(self, value, raw, rows):
        evidence = [self.index.evidence(row) for row in rows[:2]]
        if self.prefill:
            return _cell('resolved', value, method=_read_method(rows), raw=raw, evidence=evidence,
                         candidates=[{'source': 'ai', 'value': value, 'auto_accepted': False,
                                      'source_check': 'printed_in_source'}],
                         model=self.model, provider=self.provider)
        return _cell('resolved', value, method='VLM', raw=raw, evidence=evidence,
                     model=self.model, provider=self.provider)


_TWO_DECIMALS = re.compile(r'(?<![\d.,])\d{1,3}(?:,\d{3})*\.\d{2}(?![\d.,])|(?<![\d.,])\d+\.\d{2}(?![\d.,])')
_BILL_LABELS = {
    'amount_due': re.compile(r'amount\s+due|perlu\s+dibayar|total\s+payable|amount\s+payable', re.I),
    'subtotal': re.compile(r'current\s+charges|caj\s+semasa|sub[\s-]?total|jumlah\s+kecil', re.I),
}
# Lines that change what is payable: the amount due is not read as the total when any is printed.
_BALANCE_ADJUSTMENT = re.compile(
    r'arrears|tunggakan|outstanding|previous\s+balance|baki|payment\s+received|bayaran\s+diterima|deposit|'
    r'rebate|rebat|adjustment|pelarasan|credit|kredit|overpaid|lebihan|discount|diskaun|rounding|pembundaran', re.I)


def _labelled_amount(text, label):
    """The one two-decimal amount printed on the one line that carries `label`, else None.

    Skipped when several lines carry the label, when that line also names a balance
    adjustment (an arrears figure could be read instead) or holds more than one amount.
    """
    hits = [line for line in text.splitlines() if label.search(line)]
    if len(hits) != 1 or _BALANCE_ADJUSTMENT.search(hits[0]):
        return None
    amounts = _TWO_DECIMALS.findall(hits[0])
    return parse_amount(amounts[0]) if len(amounts) == 1 else None


def _fill_printed(fields, key, amount, index, issues, *, prefill, check, detail):
    """Place a printed amount in an empty field with its evidence; False when it is not printed."""
    rows = index.find_amount(Decimal(amount).copy_abs()) if amount is not None else []
    if not rows:
        return False
    fields[key] = _cell('resolved', amount, method=_read_method(rows) if prefill else 'VLM',
                        raw=rows[0]['text'], evidence=[index.evidence(row) for row in rows],
                        candidates=[{'source': 'printed_figures', 'value': amount, 'auto_accepted': False,
                                     'source_check': check}] if prefill else [])
    if not prefill:
        fields[key]['source_check'] = check
    issues.append({'code': 'VALUE_FROM_PRINTED_FIGURES', 'field': key, 'detail': detail})
    return True


_GRAND_TOTAL = re.compile(r'grand\s+total|jumlah\s+besar|jumlah\s+keseluruhan|total\s+amount|total\s+payable|'
                          r'total\s+(?:credit|debit)|jumlah\s+(?:kredit|debit)', re.I)


def _resolved(fields, key):
    cell = fields.get(key) or {}
    return cell.get('value') if cell.get('state') == 'resolved' and cell.get('value') is not None else None


def derive_document_total(fields, text, index, issues, *, prefill):
    """Fill a missing invoice / note total from the one printed grand-total line.

    The amount must be the only amount on the only line carrying a grand-total label
    and be printed in the source. When the subtotal and tax were both read, the amount
    must also equal subtotal + tax - discount as printed: that sum is only a check, the
    stored value is the printed one. Flagged VALUE_FROM_PRINTED_FIGURES for the reviewer.
    """
    if 'total_amount' not in fields or _resolved(fields, 'total_amount') is not None:
        return fields
    hits = [line for line in text.splitlines() if _GRAND_TOTAL.search(line)]
    amounts = _TWO_DECIMALS.findall(hits[0]) if len(hits) == 1 else []
    amount = parse_amount(amounts[0]) if len(amounts) == 1 else None
    if amount is None:
        return fields
    subtotal, tax = _resolved(fields, 'subtotal'), _resolved(fields, 'tax_amount')
    if subtotal is not None and tax is not None:
        discount = _resolved(fields, 'discount') or '0'
        if Decimal(subtotal) + Decimal(tax) - abs(Decimal(discount)) != Decimal(amount):
            return fields
    _fill_printed(fields, 'total_amount', amount, index, issues, prefill=prefill, check='printed_on_grand_total_line',
                  detail='Read from the printed grand-total line. Confirm it.')
    return fields


def derive_bill_values(fields, text, index, issues, *, prefill):
    """Fill a missing bill subtotal or amount due from the document's own printed figures.

    The value placed is always an amount printed in the source; tax is never computed.
      subtotal    the amount on the one "Current Charges" / "Subtotal" line, else the
                  printed amount that equals printed total minus printed tax
      amount_due  the amount on the one "Amount Due" line, else the printed total when
                  no arrears, payment, deposit, credit or other adjustment is printed
    Each fill is flagged with VALUE_FROM_PRINTED_FIGURES for the reviewer.
    """
    def value(key):
        cell = fields.get(key) or {}
        return cell.get('value') if cell.get('state') == 'resolved' and cell.get('value') is not None else None

    def fill(key, amount, check, detail):
        return _fill_printed(fields, key, amount, index, issues, prefill=prefill, check=check, detail=detail)

    total, tax = value('total_amount'), value('tax_amount')
    if 'subtotal' in fields and value('subtotal') is None:
        amount = _labelled_amount(text, _BILL_LABELS['subtotal'])
        if not (amount and fill('subtotal', amount, 'printed_on_labelled_line',
                                'Read from the printed current-charges / subtotal line. Confirm it.')):
            if total is not None and tax is not None and Decimal(total) > Decimal(tax) >= 0:
                fill('subtotal', format(Decimal(total) - Decimal(tax), 'f'), 'printed_total_less_printed_tax',
                     'The printed amount equal to the printed total less the printed tax. Confirm it.')
    if 'amount_due' in fields and value('amount_due') is None:
        amount = _labelled_amount(text, _BILL_LABELS['amount_due'])
        if not (amount and fill('amount_due', amount, 'printed_on_labelled_line',
                                'Read from the printed amount-due line. Confirm it.')):
            if total is not None and not _BALANCE_ADJUSTMENT.search(text):
                fill('amount_due', total, 'printed_total_no_adjustment_printed',
                     'No arrears, payment or adjustment is printed, so the amount due is the printed total. Confirm it.')
    return fields


def prefill_policy(environ=None):
    env = os.environ if environ is None else environ
    policy = env.get('AI_PREFILL', 'verified').strip().lower() or 'verified'
    if policy not in {'verified', 'never'}:
        raise text_llm.TextModelError('AI_PREFILL_INVALID')
    return policy


def structure_document(document, *, providers=None, environ=None):
    """Ask the text model for fields and return verified contract cells.

    Returns {'document_type', 'fields', 'line_items', 'issues', 'ai'}. Raises
    text_llm.TextModelError when no provider produced a usable answer.
    """
    env = os.environ if environ is None else environ
    policy = prefill_policy(env)
    date_order = env.get('DATE_ORDER', 'DMY').strip().upper() or 'DMY'
    if date_order not in {'DMY', 'MDY'}:
        raise text_llm.TextModelError('DATE_ORDER_INVALID')
    text, line_rows = layout_text(document)
    truncated = len(text) > MAX_PROMPT_CHARACTERS
    masker = Masker()
    masked = masker.mask_text(text[:MAX_PROMPT_CHARACTERS])
    answer, record = text_llm.complete_json(SYSTEM_PROMPT, _user_prompt(masked), providers=providers)
    record = {**record, 'mode': 'text', 'structure_version': VERSION, 'prefill_policy': policy,
              'masked_identifiers': masker.summary(), 'prompt_characters': len(masked),
              'prompt_sha256': hashlib.sha256(masked.encode('utf-8')).hexdigest(),
              'image_sent': False}
    default_method = 'OCR' if any(row.get('from_ocr') for row in document['rows']) else 'PARSER'
    resolver = _Resolver(SourceIndex(document, line_rows), masker, record, prefill=policy == 'verified',
                         date_order=date_order, default_method=default_method)
    kind = _text(answer.get('document_type'))
    kind = kind.casefold().replace(' ', '_').replace('-', '_') if kind else 'unknown'
    classified = kind in KINDS
    document_type = {'classifier_version': VERSION, 'method': 'text_model',
                     'value': kind if classified else 'unknown',
                     'status': 'classified' if classified else 'unknown',
                     'reason': 'MODEL_CLASSIFIED' if classified else 'MODEL_COULD_NOT_CLASSIFY',
                     'candidates': [], 'unsupported_headings': [], 'review_required': True}
    # Some models put the fields at the top level instead of under "fields".
    answered = answer.get('fields') if isinstance(answer.get('fields'), dict) else answer
    fields = {}
    for model_name, source_key in PROFILE_FIELDS.get(kind, {}).items():
        fields[source_key] = resolver.field(model_name, answered.get(model_name))
    if kind in {'invoice', 'credit_note', 'debit_note'}:
        fields = derive_document_total(fields, text[:MAX_PROMPT_CHARACTERS], resolver.index, resolver.issues,
                                       prefill=policy == 'verified')
    if kind == 'bill':
        fields = derive_bill_values(fields, text[:MAX_PROMPT_CHARACTERS], resolver.index, resolver.issues,
                                    prefill=policy == 'verified')
    lines = []
    items = answer.get('line_items') if isinstance(answer.get('line_items'), list) else []
    for item in items[:MAX_LINE_ITEMS] if classified else []:
        cells = resolver.line(item)
        if cells is not None:
            lines.append(cells)
    issues = list(resolver.issues)
    if truncated:
        issues.append({'code': 'DOCUMENT_TEXT_TRUNCATED',
                       'detail': 'Only the first part of a long document was examined; review the remaining pages.'})
    if len(items) > MAX_LINE_ITEMS:
        issues.append({'code': 'LINE_ITEM_LIMIT', 'detail': f'Only the first {MAX_LINE_ITEMS} line items were kept.'})
    record['outcomes'] = dict(resolver.outcomes, line_items=len(lines))
    return {'document_type': document_type, 'fields': fields, 'line_items': lines, 'issues': issues, 'ai': record}
