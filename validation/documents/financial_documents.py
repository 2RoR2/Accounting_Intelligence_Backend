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


PRETAX_BASIS_RULE = 'PRETAX-BASIS'
_ADJUSTMENTS = ('discount', 'allowances', 'charges', 'prepaid', 'rounding')


def pretax_basis(extraction, fields, values, items):
    """(amount, reason): when a printed tax-exclusive total can stand in for arithmetic.

    Subtotal is not filled in: this amount is used only as the invoice's pre-tax
    basis for validation, so the same line-sum and total checks still run and can
    still fail. Extraction reports the source structure; the current values are
    rechecked here, so a later correction that adds a discount revokes the basis.
    """
    if values.get('subtotal') is not None:
        return None, None
    rows = {FIELD_KEYS.get(row['field_key']): row for row in fields}
    net = rows.get('total_excluding_tax')
    if net is None or values.get('total_excluding_tax') is None:
        return None, None
    structure = ((extraction.get('raw_json') or {}).get('pretax_structure') or {})
    if structure.get('version') != 'pretax-structure/1':
        return None, 'Tax-exclusive total has no recorded source structure; subtotal remains required.'
    if structure.get('eligible') is not True:
        return None, 'Source structure does not establish a pre-tax basis: ' + ', '.join(structure.get('blockers') or ['UNKNOWN']) + '.'
    subtotal = rows.get('subtotal')
    if subtotal is not None and subtotal.get('extraction_state') not in {None, 'missing'}:
        return None, 'Subtotal has unresolved source candidates; review them first.'
    corrected = net.get('corrected_by') is not None and net.get('corrected_at') is not None
    if not corrected and not any(e.get('source_ref') and e.get('page') and e.get('bbox')
                                 for e in net.get('evidence') or [] if isinstance(e, dict)):
        return None, 'Tax-exclusive total lacks complete source evidence.'
    for key in _ADJUSTMENTS:
        row = rows.get(key)
        if values.get(key) is not None or (row is not None and row.get('extraction_state') not in {None, 'missing'}):
            return None, f'A {key} amount is present, so the tax-exclusive total may not equal the pre-tax basis.'
    if not items or any(item.get('amount') is None for item in items):
        return None, 'Line amounts are incomplete, so the tax-exclusive total cannot be compared with them.'
    if values.get('tax') is None or values.get('total') is None:
        return None, 'Tax and total are required before the tax-exclusive total can be used.'
    return values['total_excluding_tax'], None


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
        # A field stored with no value is present here as None, which must also fall back to the default.
        value = values.get(key) if values.get(key) is not None else default
        return Decimal(str(value)) if value is not None else None

    line_net = sum((Decimal(str(row['amount'])) for row in items if row.get('amount') is not None), Decimal('0'))
    total_tax = money('tax', Decimal('0'))
    expected_excluding = line_net - money('allowances', Decimal('0')) + money('charges', Decimal('0'))
    expected_including = expected_excluding + total_tax
    expected_due = expected_including - money('prepaid', Decimal('0')) + money('rounding', Decimal('0'))
    errors, checks = [], []

    def compare(rule, field, expected, actual):
        if actual is None:
            # Nothing printed to compare. Reported, but an absent optional amount cannot fail the document.
            checks.append({'rule': rule, 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': False,
                           'message': f'{field} is not available in the source, so this comparison does not apply.'})
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
        checks.append({'rule': 'BR-CO-14', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': False,
                       'message': 'Tax categories were not supplied, so this comparison does not apply.'})
    return {'errors': errors, 'checks': checks}


def lines_equal_total(lines, all_amounts, amounts, values=None):
    """Printed item amounts, with any printed charges and rounding, add up to the printed total, within the existing tolerance."""
    if not all_amounts or amounts.get('total') is None:
        return False
    adjustments = sum((Decimal(values[key]) for key in ('charges', 'rounding') if (values or {}).get(key) is not None), Decimal('0'))
    return abs(sum((row['amount'] for row in lines), Decimal('0')) + adjustments - amounts['total']) <= AMOUNT_TOLERANCE


def printed_total_equation(values):
    """The exact sum of the amounts a document prints, or None without a pre-tax amount and a total.

    Exact Decimal. Rounding is applied once, as printed, after everything else, and is never
    computed. An amount that is not printed is simply not a part: no zero tax is invented here.
    A tax-exclusive total already includes charges and discounts, so they are not applied twice.
    """
    printed_subtotal = values.get('subtotal') is not None
    base = values['subtotal'] if printed_subtotal else values.get('total_excluding_tax')
    if base is None or values.get('total') is None:
        return None
    expected = Decimal(base)
    text = str(base)
    parts = (('tax', 'tax', 1), ('charges', 'charges', 1), ('allowances', 'allowances', -1), ('discount', 'discount', -1), ('rounding', 'rounding', 1))
    for key, label, sign in parts if printed_subtotal else (parts[0], parts[-1]):
        if values.get(key) is not None:
            part = Decimal(values[key])
            expected += sign * part
            text += f" {'+' if sign * part >= 0 else '-'} {abs(part)} {label}"
    return {'expected': expected, 'total': Decimal(values['total']), 'text': f"{text} = {expected}"}


def derived_subtotal(values, items, fields=(), raw=None):
    """The sum of complete line amounts, when every printed amount then reaches the total exactly.

    Validation input only: the stored subtotal stays empty. If lines are tax-inclusive, or a
    printed tax-exclusive total disagrees, the sum does not come out and nothing is derived.
    An unsettled tax-exclusive total, or adjustment wording that was not read as an amount,
    means the sum cannot be relied on, so a person looks instead.
    """
    if values.get('subtotal') is not None or values.get('total') is None or not items:
        return None
    rows = {FIELD_KEYS.get(row['field_key']): row for row in fields}
    if any((rows.get(key) or {}).get('extraction_state') in {'ambiguous', 'conflict', 'unsupported', 'unreadable'}
           for key in ('subtotal', 'total_excluding_tax', 'tax')):
        return None
    blockers = ((raw or {}).get('pretax_structure') or {}).get('blockers') or []
    if 'ADJUSTMENT_TEXT_PRINTED' in blockers and not any(values.get(key) is not None for key in ('charges', 'allowances', 'discount')):
        return None
    if any(row.get('amount') is None for row in items):
        return None
    lines = sum((Decimal(str(row['amount'])) for row in items), Decimal('0'))
    net = lines
    for key, sign in (('charges', 1), ('allowances', -1), ('discount', -1)):
        if values.get(key) is not None:
            net += sign * Decimal(values[key])
    if values.get('total_excluding_tax') is not None and net != Decimal(values['total_excluding_tax']):
        return None
    reached = net + Decimal(values.get('tax') or '0') + Decimal(values.get('rounding') or '0')
    return str(lines) if reached == Decimal(values['total']) else None


# What the document says about tax. Only "printed" and "printed_zero" are amounts; the others never store a zero.
TAX_PRINTED, TAX_PRINTED_ZERO, TAX_NOT_PRINTED, TAX_EXEMPT, TAX_INCLUSIVE, TAX_UNKNOWN = (
    'printed', 'printed_zero', 'not_printed', 'exempt_stated', 'tax_inclusive', 'unknown')


def tax_status(fields, values, raw):
    """Keep apart: a printed zero, a stated exemption, tax inside the prices, no mention, and an unreadable value."""
    if values.get('tax') is not None:
        return TAX_PRINTED_ZERO if Decimal(values['tax']) == 0 else TAX_PRINTED
    row = next((row for row in fields if FIELD_KEYS.get(row['field_key']) == 'tax'), None)
    if row is not None and (row.get('extraction_state') not in {None, 'missing'} or row.get('candidates')):
        return TAX_UNKNOWN  # Something is printed or suggested, so it must be settled, not skipped.
    statement = ((raw or {}).get('tax_statement') or {}).get('status')
    return {'exempt': TAX_EXEMPT, 'inclusive': TAX_INCLUSIVE}.get(statement, TAX_NOT_PRINTED)


def tax_status_check(status, raw, *, reconciles, required, reviewed=None):
    """The TAX-STATUS result, or None where another rule already reports the problem."""
    def result(state, message, needed=False):
        return {'rule': 'TAX-STATUS', 'field': 'tax', 'result': 'PASS' if state == 'CHECKED' else 'WARNING',
                'checkStatus': state, 'required': needed, 'message': message}
    said = ((raw or {}).get('tax_statement') or {}).get('text')
    if status == TAX_PRINTED_ZERO:
        return result('CHECKED', 'The document prints a tax amount of zero.')
    if status == TAX_PRINTED:
        return result('CHECKED', 'The document prints a tax amount.')
    if status == TAX_UNKNOWN:
        return None
    if status == TAX_EXEMPT and (reconciles or not required):
        return result('CHECKED', f'The document says "{said}". No tax amount applies, and none was stored.')
    if not reconciles and required:
        return None  # The required-field rule fails tax, because the amounts do not settle it either.
    if reviewed:
        return result('CHECKED', f"No tax amount is printed. Tax stays empty. Confirmed on review by {reviewed.get('reviewed_by')}: {reviewed.get('note')}")
    if status == TAX_INCLUSIVE:
        return result('NOT_CHECKED', f'The document says "{said}", so tax is inside the amounts and cannot be separated. Tax stays empty. '
                      'An accountant must confirm this before it can be approved.', required)
    return result('NOT_CHECKED', 'No tax amount or exemption is printed. The printed amounts add up without one, but prices may still include tax. '
                  'Tax stays empty. An accountant must confirm this before it can be approved.', required)


def bill_amount_due(values):
    """(state, message), reconciling a printed amount due from the printed components of this bill.

    Signed amounts stay as printed. A payment or credit reduces what is due whichever sign it
    is printed with. Nothing is offset against another document.
    """
    due = values.get('amount_due')
    if due is None:
        return 'NOT_CHECKED', 'No amount due is printed, so what is owed is not established. It is not assumed to equal the bill total.'
    paid = values.get('payment_amount') if values.get('payment_amount') is not None else values.get('amount_paid')
    additions = [('previous balance', values.get('previous_balance')), ('arrears', values.get('arrears')),
                 ('adjustments', values.get('adjustment_total')), ('late fee', values.get('late_fee'))]
    reductions = [('payment', paid), ('credits', values.get('credit_total'))]
    current = values.get('charges') if values.get('charges') is not None else values.get('current_charges')
    base_label, base = ('current charges', current) if current is not None else ('bill total', values.get('total'))
    if base is None:
        return 'NOT_CHECKED', 'Amount due cannot be reconciled: no current charges or bill total is printed.'
    expected, text = Decimal(base), f'{base} {base_label}'
    for label, value in additions:
        if value is not None:
            expected += Decimal(value)
            text += f" {'+' if Decimal(value) >= 0 else '-'} {abs(Decimal(value))} {label}"
    for label, value in reductions:
        if value is not None:
            expected -= abs(Decimal(value))
            text += f' - {abs(Decimal(value))} {label}'
    if abs(expected - Decimal(due)) <= AMOUNT_TOLERANCE:
        return 'CHECKED', f'Printed amounts reconcile: {text} = {expected}, the printed amount due.'
    if current is not None:
        return 'FAILED', f'Printed amounts give {text} = {expected}, but the printed amount due is {due}.'
    # A bill total may already include some of these, so a difference here does not prove an error.
    return 'NOT_CHECKED', (f'Amount due {due} differs from {text} = {expected}, and a bill total may already include some of these. '
                           'Printed current charges are needed to reconcile it.')


def coverage_complete(checks):
    """Every mandatory rule that applies was actually evaluated. Separate from whether it passed."""
    return all(check.get('required', True) is False or check.get('checkStatus') in {'CHECKED', 'NOT_APPLICABLE'}
               for check in checks)


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

    def not_applicable(rule, message):
        checks.append({'rule': f'{prefix}-{rule}', 'result': 'NOT_APPLICABLE', 'checkStatus': 'NOT_APPLICABLE',
                       'required': False, 'message': message})

    # Some bills and receipts print only a total. Zero lines is right for those, and wrong if item rows are visible.
    raw_extraction = extraction.get('raw_json') or {}
    item_structure = (raw_extraction.get('item_structure') or {}).get('status')
    non_itemized = kind in {'bill', 'receipt'} and not items and item_structure == 'not_itemized'

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
    if non_itemized:
        not_applicable('LINE_DETAILS', 'The document prints no item rows, so none are required.')
        not_applicable('LINE_MATH', 'The document prints no item rows.')
    elif kind in {'bill', 'receipt'} and not items:
        check('LINE_DETAILS', 'Item rows are visible on the document but none were read. Review it.' if item_structure == 'unmapped_rows'
              else 'No item rows were read, and whether this document prints any is not recorded. Review it.', available=False)
        check('LINE_MATH', 'No item rows were read.', available=False, required=False)
    elif kind in {'bill', 'receipt'}:
        check('LINE_DETAILS', 'Every line has a description and an amount.' if descriptions_available and all_amounts else
              'A line is missing its description or amount; inspect the source items.', available=descriptions_available and all_amounts)
        # Quantity and price are required only where printed, and a missing one never counts as zero.
        priced = any(row['quantity'] is not None and row['unit_price'] is not None for row in lines)
        if not any(e['code'] == prefix + '-LINE_MATH' for e in errors):
            if priced:
                check('LINE_MATH', 'Quantity x unit price agrees with the amount on every line that prints both.')
            else:
                not_applicable('LINE_MATH', 'No line prints both a quantity and a unit price.')
    else:
        check('LINE_DETAILS', 'Line descriptions are available.' if descriptions_available else
              'Line descriptions are incomplete; inspect source items.', available=descriptions_available, required=False)
        if not any(e['code'] == prefix + '-LINE_MATH' for e in errors):
            check('LINE_MATH', 'Line quantity × unit price agrees with amount.' if all_line_math else
                  'Line quantity/unit-price/amount evidence is incomplete; no defaults inferred.',
                  available=all_line_math, required=kind == 'receipt')
    if all_amounts and amounts.get('subtotal') is not None:
        line_sum = sum((row['amount'] for row in lines), Decimal('0'))
        printed_tax = amounts.get('tax') or Decimal('0')
        if kind == 'receipt' and printed_tax and abs(line_sum - amounts['subtotal']) > AMOUNT_TOLERANCE and (
                abs(line_sum - amounts['subtotal'] - printed_tax) <= AMOUNT_TOLERANCE):
            # A retail receipt prices items with tax in them, so its lines reach subtotal + tax, not subtotal.
            check('SUBTOTAL', f'The item amounts include tax: {line_sum} = {amounts["subtotal"]} + {printed_tax} tax.')
        elif abs(line_sum - amounts['subtotal']) > AMOUNT_TOLERANCE:
            issue('SUBTOTAL', 'subtotal', f'Line sum {line_sum} differs from subtotal {amounts["subtotal"]}.', 'high')
        else:
            check('SUBTOTAL', 'Sum of evidenced line amounts agrees with subtotal.')
    elif kind == 'receipt' and lines_equal_total(lines, all_amounts, amounts, values):
        check('SUBTOTAL', 'No separate subtotal is printed. The item amounts add up to the printed total.')
    elif non_itemized:
        not_applicable('SUBTOTAL', 'The document prints no item rows to compare with a subtotal.')
    elif kind in {'receipt', 'credit_note', 'debit_note'}:
        check('SUBTOTAL', 'Line amounts and explicit subtotal are required for this comparison.', available=False)

    if kind == 'bill':
        state, message = bill_amount_due(values)
        if state == 'FAILED':
            issue('AMOUNT_DUE', 'amount_due', message, 'high')
        else:
            check('AMOUNT_DUE', message, available=state == 'CHECKED')
    else:
        # No zero is invented for absent tax/subtotal. An explicit discount, if
        # present in the current contract, participates separately in the math.
        raw = extraction.get('raw_json') or {}
        evidence_text = ' '.join(ev.get('text', '') for row in fields for ev in row.get('evidence', []))
        # An adjustment that was read as its own amount takes part in the sum below, so it is no longer unsupported.
        untyped = ['credit', 'tip', 'surcharge'] + ([] if values.get('rounding') is not None else ['rounding']) + (
            [] if values.get('charges') is not None else ['service charge'])
        unsupported_adjustment = re.search(r'\b(' + '|'.join(untyped) + r')\s*:', evidence_text, re.I)
        unsupported_adjustment = unsupported_adjustment or (
            amounts.get('discount') is None and re.search(r'\bdiscount\s*:', evidence_text, re.I))
        unsupported_table = any(issue.get('code') == 'UNSUPPORTED_TABLE' for issue in raw.get('issues', []))
        if kind in {'credit_note', 'debit_note'} and any(number < 0 for number in amounts.values()):
            issue('SIGN', 'total', 'Credit/debit note amounts must be non-negative; the document kind carries the adjustment direction.', 'high')
        equation = printed_total_equation(values) if kind == 'receipt' else None
        rounding = Decimal(values.get('rounding') or '0')
        inclusive = values.get('total_including_tax')
        labelled_inclusive = kind == 'receipt' and any(
            FIELD_KEYS.get(row['field_key']) == 'total' and re.search(r'\bincl', str(row.get('raw_value') or ''), re.I) for row in fields)
        if equation and inclusive is not None and values.get('tax') is not None:
            # The printed tax-inclusive amount sits before rounding, so it is checked before it.
            before_rounding = equation['expected'] - rounding
            if abs(before_rounding - Decimal(inclusive)) > AMOUNT_TOLERANCE:
                issue('TOTAL', 'total_including_tax', f'Printed amounts before rounding give {before_rounding}; '
                      f'the printed tax-inclusive total is {inclusive}.', 'high')
        if kind == 'receipt' and inclusive is not None and values.get('total') is not None and (
                abs(Decimal(inclusive) + rounding - Decimal(values['total'])) > AMOUNT_TOLERANCE):
            issue('TOTAL', 'total', f'{inclusive} tax-inclusive total with {rounding} rounding does not reach the printed total {values["total"]}.', 'high')
        if (equation and values.get('tax') is not None and values.get('subtotal') is not None and labelled_inclusive
                and abs(Decimal(values['subtotal']) + rounding - equation['total']) <= AMOUNT_TOLERANCE):
            # Tax is never added to an amount that already includes it.
            check('TOTAL', f'The total is printed inclusive of tax, and the amount entered as subtotal ({values["subtotal"]}) equals it, '
                  f'so the {values["tax"]} tax is already inside and was not added again. The tax-exclusive base would be '
                  f'{Decimal(values["subtotal"]) - Decimal(values["tax"])}.')
        elif equation and not unsupported_adjustment and not unsupported_table and (
                values.get('tax') is not None or equation['expected'] == equation['total']):
            if abs(equation['expected'] - equation['total']) > AMOUNT_TOLERANCE:
                issue('TOTAL', 'total', f'Printed amounts give {equation["text"]}; the printed total is {equation["total"]}.', 'high')
            else:
                check('TOTAL', f'Printed amounts reconcile: {equation["text"]}.')
        elif kind == 'receipt' and equation is None and inclusive is not None and values.get('total') is not None and (
                abs(Decimal(inclusive) + rounding - Decimal(values['total'])) <= AMOUNT_TOLERANCE):
            check('TOTAL', f'Printed amounts reconcile: {inclusive} tax-inclusive total + {rounding} rounding = {values["total"]}. Tax is not added again.')
        elif kind == 'receipt' and amounts.get('subtotal') is None and lines_equal_total(lines, all_amounts, amounts, values):
            check('TOTAL', 'The printed item amounts add up to the printed total. Any tax inside them is not separately stated.')
        elif non_itemized and not equation and values.get('tax') is None:
            not_applicable('TOTAL', 'Only a total is printed, so there are no amounts to compare it with.')
        elif unsupported_adjustment or unsupported_table:
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
    if kind in {'bill', 'receipt'}:
        tax = tax_status_check(tax_status(fields, values, raw_extraction), raw_extraction, reconciles=True, required=False)
        if tax:
            checks.append(tax)
    return {'errors': errors, 'checks': checks,
            'coverage_complete': coverage_complete(checks), 'validation_passed': not errors,
            'review_required': True, 'accounting_approval_performed': False}
