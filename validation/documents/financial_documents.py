"""Bounded bill/receipt checks over current persisted values, never approval.

Bill total, amount_due and current charges remain distinct. Until
balance/payment/credit components have a typed contract, reconciliation is
explicitly NOT_CHECKED. Receipt math covers simple item/subtotal/tax layouts.
"""
from datetime import date
from decimal import Decimal
import re

from services.extraction.contracts.accounting_contract import FIELD_KEYS, decimal_text, record_values
from validation.documents.models import AMOUNT_TOLERANCE


def evaluate_complex_invoice_financials(extraction, fields, items):
    """Evaluate extended totals when optional components are evidenced."""
    values = {}
    for row in fields:
        try:
            key = FIELD_KEYS.get(row['field_key'], row['field_key'])
            values[key] = record_values([row]).get(key)
        except (ValueError, TypeError):
            continue
    extended_keys = ('allowances', 'charges', 'prepaid', 'rounding',
                     'total_excluding_tax', 'total_including_tax', 'amount_due')
    if not any(values.get(key) is not None for key in extended_keys):
        return {'errors': [], 'checks': []}

    def money(key, default=None):
        value = values.get(key, default)
        return Decimal(str(value)) if value is not None else None

    line_net = sum((Decimal(str(row['amount'])) for row in items if row.get('amount') is not None), Decimal('0'))
    total_tax = money('tax', Decimal('0'))
    expected_excluding = line_net - money('allowances', Decimal('0')) + money('charges', Decimal('0'))
    expected_including = expected_excluding + total_tax
    expected_due = expected_including - money('prepaid', Decimal('0')) + money('rounding', Decimal('0'))
    errors, checks = [], []

    def compare(rule, field, expected, actual):
        if actual is None:
            checks.append({'rule': rule, 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
                           'message': f'{field} is not available in the source.'})
        elif abs(expected - actual) > AMOUNT_TOLERANCE:
            errors.append({'code': rule, 'field': field, 'severity': 'high',
                           'message': f'Expected {expected} but received {actual}.'})
        else:
            checks.append({'rule': rule, 'result': 'PASS', 'checkStatus': 'CHECKED',
                           'message': f'{field} reconciles to {expected}.'})

    compare('BR-CO-10', 'line_net_total', line_net, money('subtotal'))
    compare('BR-CO-13', 'total_excluding_tax', expected_excluding, money('total_excluding_tax'))
    compare('BR-CO-15', 'total_including_tax', expected_including, money('total_including_tax'))
    compare('BR-CO-16', 'amount_due', expected_due, money('amount_due'))

    categories = (extraction.get('raw_json') or {}).get('tax_categories') or []
    if categories:
        category_total = Decimal('0')
        for category in categories:
            try:
                taxable = Decimal(str(category['taxable_amount']))
                rate = Decimal(str(category['tax_rate']))
                reported = Decimal(str(category['tax_amount']))
                expected = (taxable * rate / Decimal('100')).quantize(Decimal('0.01'))
                if abs(expected - reported) > AMOUNT_TOLERANCE:
                    errors.append({'code': 'BR-CO-17', 'field': 'tax_categories', 'severity': 'high',
                                   'message': f'Tax category expected {expected} but received {reported}.'})
                category_total += reported
            except (KeyError, TypeError, ValueError, ArithmeticError):
                errors.append({'code': 'BR-CO-17', 'field': 'tax_categories', 'severity': 'high',
                               'message': 'Tax category has invalid taxable amount, rate, or tax amount.'})
        compare('BR-CO-14', 'total_tax', category_total, total_tax)
    else:
        checks.append({'rule': 'BR-CO-14', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
                       'message': 'Tax categories were not supplied.'})
    return {'errors': errors, 'checks': checks}


def evaluate_financial_document(kind, extraction, fields, items):
    prefix = kind.upper()
    errors, checks, values = [], [], {}
    invalid = set()

    def issue(rule, field, message, severity='medium'):
        errors.append({'code': f'{prefix}-{rule}', 'field': field,
                       'message': message, 'severity': severity})

    def check(rule, message, *, available=True, required=True):
        checks.append({'rule': f'{prefix}-{rule}', 'result': 'PASS' if available else 'WARNING',
                       'checkStatus': 'CHECKED' if available else 'NOT_CHECKED',
                       'required': required, 'message': message})

    for row in fields:
        key = FIELD_KEYS.get(row['field_key'], row['field_key'])
        try:
            if key in values:
                raise ValueError('Conflicting stored aliases; review source and current values.')
            values.update(record_values([row]))
            corrected = row.get('corrected_by') is not None and row.get('corrected_at') is not None
            if not corrected and values.get(key) is None and row.get('extraction_state') in {
                    'ambiguous', 'conflict', 'unsupported', 'unreadable'}:
                raise ValueError('Unresolved field format or candidates; inspect source evidence.')
        except ValueError as exc:
            values[key] = None
            invalid.add(key)
            issue('FORMAT', key, str(exc))

    # The current canonical contract keeps bill total and amount_due separate.
    # An explicit due amount satisfies the existing obligation-amount check;
    # it is never copied into total/current charges or used to infer settlement.
    amount_key = 'amount_due' if kind == 'bill' and (
        values.get('amount_due') is not None or 'amount_due' in invalid) else 'total'
    required = ('supplier', 'date', 'currency', amount_key)
    for key in required:
        if values.get(key) is None and key not in invalid:
            issue('REQUIRED', key, 'Missing evidenced ' + (
                {'supplier': 'issuer' if kind == 'bill' else 'merchant',
                 'date': 'bill date' if kind == 'bill' else 'transaction date',
                 'total': 'amount due' if kind == 'bill' else 'total'}.get(key, key)) + '.')
    # A bill may identify an account rather than carry an invoice-style number.
    if kind == 'bill' and not (values.get('reference') or values.get('bill_reference')
                               or values.get('bill_number') or values.get('account_number')):
        issue('REQUIRED', 'reference', 'An evidenced bill reference or account number is required.')
    if kind in {'credit_note', 'debit_note'}:
        if not (values.get('reference') or values.get('invoice') or values.get('credit_note_number') or values.get('debit_note_number')):
            issue('REQUIRED', 'reference', f'An evidenced {kind.replace("_", " ")} number or related invoice reference is required.')
        if values.get('date') and values.get('due') and values['due'] < values['date']:
            issue('DATES', 'due', 'Due date precedes the note date.')
    if not errors:
        check('FIELDS', 'Required identity/date/currency/amount fields have supported formats.')

    amounts = {}
    for key in ('subtotal', 'tax', 'discount', 'total'):
        value = values.get(key)
        if value is not None:
            amounts[key] = Decimal(value)
    # Returns/credits need an explicit policy. A signed amount is not itself a
    # malformed decimal; preserve it and mark that accounting scope unchecked.
    if kind == 'receipt':
        if any(number < 0 for number in amounts.values()):
            check('RETURNS', 'Signed receipt amounts require a return/credit policy; not checked.', available=False)

    if kind == 'bill':
        if values.get('date') and values.get('due'):
            if values['due'] < values['date']:
                issue('DATES', 'due', 'Due date precedes the evidenced bill date.')
            else:
                check('DUE_DATE', 'Due date is on or after bill date.', required=False)
        else:
            check('DUE_DATE', 'Bill/due date evidence is incomplete.', available=False, required=False)
        period = values.get('billing_period')
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})', period or '')
        if match:
            try:
                start, end = (date.fromisoformat(value) for value in match.groups())
                if start > end:
                    issue('DATES', 'billing_period', 'Billing-period start is after its end.')
                else:
                    check('BILLING_PERIOD', 'Evidenced billing-period dates are ordered.', required=False)
            except ValueError:
                issue('FORMAT', 'billing_period', 'Billing period contains an invalid calendar date.')
        else:
            check('BILLING_PERIOD', 'No supported ISO start to end billing-period evidence.',
                  available=False, required=period is not None)

    lines = []
    all_line_math = bool(items)
    all_amounts = bool(items)
    descriptions_available = bool(items)
    for index, row in enumerate(items):
        description = row.get('description')
        descriptions_available = descriptions_available and isinstance(description, str) and bool(description.strip())
        current = {}
        for key, scale in (('quantity', 3), ('unit_price', 4), ('amount', 2)):
            value = row.get(key)
            field = f'line_items.{index}.{key}'
            try:
                if value is None:
                    if (row.get('field_states') or {}).get(key) in {'ambiguous', 'conflict', 'unsupported', 'unreadable'}:
                        raise ValueError('Unresolved line field; inspect source evidence.')
                    current[key] = None
                else:
                    current[key] = Decimal(decimal_text(value, 14, scale))
                    if kind == 'receipt' and (current[key] < 0 or key == 'quantity' and current[key] == 0):
                        check('RETURNS', f'{field}: signed amounts or nonpositive quantities need an explicit return/adjustment policy.',
                              available=False)
            except ValueError as exc:
                current[key] = None
                issue('FORMAT', field, str(exc))
        lines.append(current)
        all_amounts = all_amounts and current['amount'] is not None
        if all(current[key] is not None for key in ('quantity', 'unit_price', 'amount')):
            expected = round(current['quantity'] * current['unit_price'], 2)
            if abs(expected - current['amount']) > AMOUNT_TOLERANCE:
                issue('LINE_MATH', f'line_items.{index}.amount',
                      f'Line amount {current["amount"]} differs from quantity × unit price {expected}.', 'high')
        else:
            all_line_math = False
    check('LINE_DETAILS', 'Line descriptions are available.' if descriptions_available else
          'Line descriptions are incomplete; inspect source items.',
          available=descriptions_available, required=kind == 'receipt')
    if not any(e['code'] == prefix + '-LINE_MATH' for e in errors):
        check('LINE_MATH', 'Line quantity × unit price agrees with amount.' if all_line_math else
              'Line quantity/unit-price/amount evidence is incomplete; no defaults inferred.',
              available=all_line_math, required=kind == 'receipt')
    if all_amounts and amounts.get('subtotal') is not None:
        line_sum = sum((row['amount'] for row in lines), Decimal('0'))
        if abs(line_sum - amounts['subtotal']) > AMOUNT_TOLERANCE:
            issue('SUBTOTAL', 'subtotal', f'Line sum {line_sum} differs from subtotal {amounts["subtotal"]}.', 'high')
        else:
            check('SUBTOTAL', 'Sum of evidenced line amounts agrees with subtotal.')
    elif kind in {'receipt', 'credit_note', 'debit_note'}:
        check('SUBTOTAL', 'Line amounts and explicit subtotal are required for this comparison.', available=False)

    if kind == 'bill':
        check('AMOUNT_DUE', 'Amount due is not equated with charges: balance, payment and credit '
              'components are not represented by the current extraction contract.', available=False)
    else:
        # No zero is invented for absent tax/subtotal. An explicit discount, if
        # present in the current contract, participates separately in the math.
        raw = extraction.get('raw_json') or {}
        evidence_text = ' '.join(ev.get('text', '') for row in fields for ev in row.get('evidence', []))
        unsupported_adjustment = re.search(r'\b(credit|rounding|tip|surcharge|service charge)\s*:', evidence_text, re.I)
        unsupported_adjustment = unsupported_adjustment or (
            amounts.get('discount') is None and re.search(r'\bdiscount\s*:', evidence_text, re.I))
        unsupported_table = any(issue.get('code') == 'UNSUPPORTED_TABLE' for issue in raw.get('issues', []))
        if kind in {'credit_note', 'debit_note'} and any(number < 0 for number in amounts.values()):
            issue('SIGN', 'total', 'Credit/debit note amounts must be non-negative; the document kind carries the adjustment direction.', 'high')
        if unsupported_adjustment or unsupported_table:
            check('TOTAL', 'Unsupported adjustment/table evidence requires accountant review.', available=False)
        elif all(amounts.get(key) is not None for key in ('subtotal', 'tax', 'total')):
            expected = amounts['subtotal'] + amounts['tax']
            if amounts.get('discount') is not None:
                expected -= amounts['discount']
            if abs(expected - amounts['total']) > AMOUNT_TOLERANCE:
                issue('TOTAL', 'total', f'Evidenced subtotal/tax/discount components give {expected}; '
                      f'total is {amounts["total"]}.', 'high')
            else:
                check('TOTAL', 'Evidenced subtotal + tax (less explicit discount when present) agrees with total.')
        else:
            check('TOTAL', 'Explicit subtotal, tax and total are required; missing amounts remain null.', available=False)

    if kind in {'credit_note', 'debit_note'}:
        check('NOTE_LINK', 'Related invoice/reference is evidenced or the note number is present.', required=True)
        check('NOTE_DIRECTION', f'{kind.replace("_", " ").title()} direction is represented by document kind.', required=False)
    return {'errors': errors, 'checks': checks,
            'coverage_complete': not errors and all(c['checkStatus'] == 'CHECKED' for c in checks if c['required']),
            'review_required': True, 'accounting_approval_performed': False}
