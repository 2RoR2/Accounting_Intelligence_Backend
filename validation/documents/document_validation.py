"""Deterministic checks over persisted effective values; never approves records."""
from decimal import Decimal
import json
from fastapi import HTTPException
from services.extraction.contracts.accounting_contract import FIELD_KEYS, record_values
from validation.documents.engine import EXCEPTION_SEVERITY, validate_invoice_document
from validation.documents.references import (
    accepted_reviews, customer_catalogue, invoice_references, purchase_order_reference, read_or_unavailable,
    tenant_policy, transaction_references,
)
from validation.documents.models import AMOUNT_TOLERANCE
from validation.documents.financial_documents import (
    PRETAX_BASIS_RULE, TAX_EXEMPT, TAX_INCLUSIVE, TAX_NOT_PRINTED, coverage_complete, derived_subtotal,
    evaluate_complex_invoice_financials, evaluate_financial_document, pretax_basis, printed_total_equation,
    tax_status, tax_status_check,
)
from validation.documents.profiles import validate_profile
from validation.matching.vendor_matching import find_vendor_match


RULE_VERSION = 'validation-v3.1'
# Findings an accountant may settle with a recorded reason. Local-admin approval still follows.
REVIEWABLE_RULES = {'EX-010', 'TAX-STATUS'}
# A stored status cannot say "not applicable" until the schema allows it, so this marks it. See stored_status().
NOT_APPLICABLE_MARK = 'NOT_APPLICABLE'
# Duplicate checks cover invoices today. Bills and receipts need a decision: a receipt often prints no number at all.
DUPLICATE_POLICY_KINDS = {'invoice'}
_CANONICAL_ERROR_FIELDS = {'vendor_name': 'supplier', 'invoice_date': 'date', 'due_date': 'due', 'total_amount': 'total',
                           'tax_amount': 'tax', 'invoice_number': 'invoice'}
_SEVERITY_ORDER = {'low': 0, 'info': 0, 'medium': 1, 'high': 2}


CAUSE_LABELS = {
    'MISSING_SOURCE_VALUE': 'Missing from the source or not extracted',
    'INVALID_FORMAT': 'Value has an unsupported format',
    'CONFLICTING_VALUES': 'Source values conflict',
    'UNCERTAIN_SOURCE_VALUE': 'Printed but uncertain',
    'ARITHMETIC_FAILURE': 'Amounts do not reconcile',
    'UNKNOWN_VENDOR': 'Supplier not matched',
    'POSSIBLE_DUPLICATE': 'Possible duplicate',
    'REFERENCE_NOT_FOUND': 'Referenced record not found',
    'TRANSACTION_ANOMALY': 'Unusual amount',
    'CLASSIFICATION_MISMATCH': 'Document type unresolved',
    'POLICY_REQUIREMENT': 'Business rule requires review',
    'MODEL_DISAGREEMENT': 'A second reading disagrees',
}


def exception_cause(code, field=None, message=''):
    """Why a finding exists, so unlike problems are not presented as the same thing."""
    code, field, message = str(code or ''), str(field or ''), str(message or '')
    if field == 'document_type':
        return 'CLASSIFICATION_MISMATCH'
    if code == MODEL_DISAGREEMENT_RULE:
        return 'MODEL_DISAGREEMENT'
    if field == 'category' or code == 'PROFILE-001':
        return 'POLICY_REQUIREMENT'
    if code in {'EX-003', 'EX-004', 'EX-005', 'EX-006'} or code.startswith('BR-CO') or code.endswith(('-LINE_MATH', '-SUBTOTAL', '-TOTAL', '-DATES', '-SIGN')):
        return 'ARITHMETIC_FAILURE'
    if code == 'EX-007':
        return 'POSSIBLE_DUPLICATE'
    if code == 'EX-008':
        return 'UNKNOWN_VENDOR'
    if code == 'EX-009':
        return 'REFERENCE_NOT_FOUND'
    if code == 'EX-010':
        return 'TRANSACTION_ANOMALY'
    if 'candidates disagree' in message:
        return 'CONFLICTING_VALUES'
    if 'Unresolved' in message or 'symbol' in message:
        return 'UNCERTAIN_SOURCE_VALUE'
    if code in {'EX-001', 'PROFILE-REQUIRED'} or code.endswith('-REQUIRED'):
        return 'MISSING_SOURCE_VALUE'
    if code == 'EX-002' or code.endswith('-FORMAT'):
        return 'INVALID_FORMAT'
    return 'POLICY_REQUIREMENT'


MODEL_DISAGREEMENT_RULE = 'MODEL-DISAGREEMENT'


def model_disagreements(fields):
    """A parser value that a source-checked second reading contradicts needs a person's decision.

    The value is never changed here. Saving a value for the field (the same one or another)
    records who decided and when, and the field history keeps both; until then the finding
    blocks approval. Model answers the source did not support never reach the field.
    """
    errors = []
    for row in fields:
        if row.get('corrected_by') is not None and row.get('corrected_at') is not None:
            continue
        if row.get('extraction_state') != 'resolved':
            continue
        conflict = next((c for c in row.get('candidates') or [] if isinstance(c, dict) and c.get('source') == 'ai'
                         and c.get('auto_accepted') is False and c.get('outcome') == 'PARSER_MODEL_CONFLICT'), None)
        if conflict is None:
            continue
        errors.append({'code': MODEL_DISAGREEMENT_RULE, 'field': FIELD_KEYS.get(row.get('field_key'), row.get('field_key')),
                       'severity': 'high', 'model_candidate': conflict.get('value'), 'source_check': conflict.get('source_check'),
                       'message': 'A second reading of the page gives a different value. Check the source, then save the correct '
                                  'value (even if unchanged) to record your decision. Nothing was changed automatically.'})
    return errors


def _with_causes(errors):
    for error in errors:
        error['cause'] = exception_cause(error.get('code'), error.get('field'), error.get('message'))
    return errors


def _unresolved_message(row):
    """Say what is actually wrong with a printed but unresolved value."""
    values = {c.get('value') for c in row.get('candidates') or [] if isinstance(c, dict) and c.get('source') != 'ai'}
    if FIELD_KEYS.get(row.get('field_key')) == 'currency' and values == {None}:
        return 'Only a currency symbol is printed, which does not identify a currency. Confirm it from the document.'
    if len(values - {None}) > 1:
        return 'Source candidates disagree; no candidate was selected. Inspect source evidence.'
    return 'Unresolved field format or candidates; inspect source evidence.'


def independent_arithmetic(invoice, items, fields, reached):
    """Checks that need only their own prerequisites, when one unrelated field failed.

    The Invoice model stops at its first field errors, so its arithmetic never runs
    then. These are the same comparisons, in exact Decimal. Amounts are compared only
    within one document's printed unit, and never if explicit currencies conflict.
    """
    errors, checks = [], []
    def not_checked(rule, why):
        checks.append({'rule': rule, 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': False, 'message': why})
    if reached:
        return errors, checks
    currency = next((row for row in fields if FIELD_KEYS.get(row.get('field_key')) == 'currency'), None)
    printed = {c.get('value') for c in (currency or {}).get('candidates') or [] if isinstance(c, dict) and c.get('source') != 'ai'} - {None}
    if len(printed) > 1:
        not_checked('ARITHMETIC-PREREQ', 'Amounts were not compared because the document prints more than one currency.')
        return errors, checks
    def money(name):
        value = invoice.get(name)
        try:
            return Decimal(str(value)) if value is not None else None
        except ArithmeticError:
            return None
    subtotal, tax, total, discount = money('subtotal'), money('tax_amount'), money('total_amount'), money('discount')
    adjustments = money('other_adjustments') or Decimal('0')  # Signed: printed charges, allowances and rounding.
    amounts = [item.get('amount') for item in items]
    if subtotal is None:
        not_checked('EX-004', 'Line total check needs a subtotal or a supported tax-exclusive total.')
    elif not amounts or any(value is None for value in amounts):
        not_checked('EX-004', 'Line total check needs an amount on every line.')
    else:
        line_sum = sum((Decimal(str(value)) for value in amounts), Decimal('0'))
        if abs(line_sum - subtotal) > AMOUNT_TOLERANCE:
            errors.append({'code': 'EX-004', 'field': 'subtotal', 'severity': EXCEPTION_SEVERITY['EX-004'],
                           'message': f'sum(line_items.amount) = {line_sum} does not match subtotal = {subtotal}'})
        else:
            checks.append({'rule': 'EX-004', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                           'message': 'Line amounts add up to the pre-tax amount, compared in the amounts as printed.'})
    if subtotal is None or tax is None or total is None:
        not_checked('EX-005', 'Total check needs a pre-tax amount, tax and total.')
    else:
        expected = subtotal + tax - (discount or Decimal('0')) + adjustments
        if abs(expected - total) > AMOUNT_TOLERANCE:
            errors.append({'code': 'EX-005', 'field': 'total_amount', 'severity': EXCEPTION_SEVERITY['EX-005'],
                           'message': f'subtotal + tax_amount - discount{" + charges + rounding" if adjustments else ""} = {expected} does not match total_amount = {total}'})
        else:
            checks.append({'rule': 'EX-005', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                           'message': 'Pre-tax amount, tax and total agree, compared in the amounts as printed.'})
    return errors, checks


def consolidate_errors(errors):
    """One finding per problem: a profile requirement repeats a field's existing error.

    The surviving error keeps the stricter severity, so approval blocking is
    unchanged, and names the rule it absorbed. Other errors are left as they are.
    """
    def canonical(error):
        return _CANONICAL_ERROR_FIELDS.get(error.get('field'), error.get('field'))
    primary = {}
    for error in errors:
        if error.get('code') != 'PROFILE-REQUIRED':
            primary.setdefault(canonical(error), error)
    result = []
    for error in errors:
        target = primary.get(canonical(error)) if error.get('code') == 'PROFILE-REQUIRED' else None
        if target is None:
            result.append(error)
            continue
        if _SEVERITY_ORDER.get(error.get('severity'), 2) > _SEVERITY_ORDER.get(target.get('severity'), 2):
            target['severity'] = error['severity']
        target.setdefault('relatedRules', []).append(error['code'])
    return result


def _profile_values(fields):
    """Pass only unambiguous typed values to supplementary profile checks.

    The financial validator reports malformed/conflicting fields. Re-reading
    the whole set strictly here must not turn those review findings into a 500.
    Canonical snapshot conversion remains strict in accounting_contract.
    """
    values, invalid = {}, set()
    for row in fields:
        key = FIELD_KEYS.get(row['field_key'])
        try:
            if key in values or key in invalid:
                raise ValueError('Conflicting stored aliases for field.')
            values.update(record_values([row]))
        except (ValueError, TypeError):
            invalid.add(key)
            values[key] = None
    return values


def purchase_order_outcome(db, document, po_number, policy):
    """(order, check, error) for EX-009. Whether a PO is needed is one question; whether it matched is another."""
    def result(state, message, required):
        return {'rule': 'EX-009', 'result': 'WARNING' if state == 'NOT_CHECKED' else state, 'checkStatus': state,
                'required': required, 'message': message}
    if document['category'] != 'accounts_payable':
        return None, result(NOT_APPLICABLE_MARK, 'Purchase-order matching applies to payables only.', False), None
    if not po_number:
        if policy.get('require_po_for_ap'):
            return None, result('NOT_CHECKED', 'This company requires a purchase order on payables, and none is on this document. '
                                'Enter its number, or return the document.', True), None
        return None, result(NOT_APPLICABLE_MARK, 'No purchase order is printed or required, so matching does not apply.', False), None
    # A failed read is not "no such order": read_or_unavailable returns False only for that.
    order = read_or_unavailable(db, lambda: purchase_order_reference(db, document, po_number) or False)
    if order is None:
        return None, result('NOT_CHECKED', 'A purchase-order number is present, but order data could not be read. Check the order yourself, then revalidate.', True), None
    if order is False:
        return None, None, {'code': 'EX-009', 'severity': 'high', 'field': 'purchase_order_number',
                            'message': 'The referenced purchase order was not found as an active order for this tenant.'}
    return order, None, None


def evaluate_invoice(document, extraction, fields, items, db=None):
    errors = []
    def issue(field, message, code='EX-002', severity=None):
        errors.append({'code': code, 'field': field, 'message': message,
                       'severity': severity or EXCEPTION_SEVERITY[code]})
    if document['kind'] != 'invoice':
        issue('document_type', 'Deterministic invoice validation does not cover this document kind.', severity='high')
    if document['category'] not in {'accounts_payable', 'accounts_receivable'}:
        issue('category', 'Explicit AP/AR business direction is required.')
    raw = extraction.get('raw_json') or {}
    if raw.get('mode') == 'module2_public_result':
        classification = raw.get('document_type') or {}
        if classification.get('status') != 'classified' or classification.get('value') != document['kind']:
            issue('document_type', 'Source classification is unknown or disagrees with the selected kind.', severity='high')
    mapping = {'invoice':'invoice_number', 'supplier':'vendor_name', 'date':'invoice_date', 'due':'due_date',
               'subtotal':'subtotal', 'tax':'tax_amount', 'total':'total_amount', 'currency':'currency', 'discount':'discount'}
    values, invalid = {}, set()
    for row in fields:
        key = FIELD_KEYS.get(row['field_key'])
        target = mapping.get(key, key or row['field_key'])
        try:
            if key in values:
                raise ValueError('Conflicting stored aliases for field.')
            values.update(record_values([row]))
            corrected = row.get('corrected_by') is not None and row.get('corrected_at') is not None
            if not corrected and values.get(key) is None and row.get('extraction_state') in {
                    'ambiguous', 'conflict', 'unsupported', 'unreadable'}:
                raise ValueError(_unresolved_message(row))
        except ValueError as exc:
            values[key] = None
            invalid.add(target)
            issue(target, str(exc))
    invoice = {target: values[source] for source, target in mapping.items() if values.get(source) is not None}
    # Validation input only. The stored subtotal stays null and distinct.
    basis, basis_reason = pretax_basis(extraction, fields, values, items)
    derived = derived_subtotal(values, items, fields, raw) if basis is None else None
    if basis is not None:
        invoice['subtotal'] = basis
    elif derived is not None:
        invoice['subtotal'] = derived
    # Printed charges, allowances and rounding are part of how the total was reached. Validation input only.
    adjustments = sum((sign * Decimal(values[key]) for key, sign in (('charges', 1), ('allowances', -1), ('rounding', 1))
                       if values.get(key) is not None), Decimal('0'))
    if adjustments:
        invoice['other_adjustments'] = str(adjustments)
    equation = printed_total_equation({**values, 'subtotal': invoice.get('subtotal')})
    reconciles = equation is not None and equation['expected'] == equation['total']
    tax_state = tax_status(fields, values, raw)
    reviewed = accepted_reviews(db, document) if db is not None else {}
    if tax_state in {TAX_NOT_PRINTED, TAX_EXEMPT, TAX_INCLUSIVE} and reconciles:
        invoice['tax_amount'] = '0.00'  # Lets the same arithmetic run. Never stored: tax stays empty, as printed.
    invoice['purchase_order_number'] = values.get('purchase_order_number')
    invoice['line_items'] = [{key: row.get(key) for key in ('description','quantity','unit_price','amount')} for row in items]
    for index, row in enumerate(items):
        for key, state in (row.get('field_states') or {}).items():
            if row.get(key) is None and state in {'ambiguous','conflict','unsupported','unreadable'}:
                target = f'line_items.{index}.{key}'
                invalid.add(target)
                issue(target, 'Unresolved line field format; inspect source evidence.')
    invoice['document_id'] = str(document['id']) if document.get('id') else None
    invoice['source_file'] = invoice['document_id']
    if document['category'] == 'accounts_receivable' and values.get('customer') is None:
        issue('customer', 'Receivable invoices require an explicit customer.', 'EX-001')
    policy = tenant_policy(db, document)
    vendors, existing = read_or_unavailable(db, lambda: invoice_references(db, document), (None, None)) or (None, None)
    receivable = document['category'] == 'accounts_receivable'
    history = read_or_unavailable(db, lambda: transaction_references(db, document, values.get('expense_category'),
                                                                     values.get('customer') if receivable else values.get('supplier')))
    # On a receivable the seller is this company, so it is the customer that gets matched.
    catalogue = read_or_unavailable(db, lambda: customer_catalogue(db, document)) if receivable else vendors
    po_number = values.get('purchase_order_number')
    purchase_order, po_check, po_error = purchase_order_outcome(db, document, po_number, policy)
    result = validate_invoice_document(invoice, known_vendors=catalogue, existing_invoices=existing,
                                       reference_transactions=history, purchase_order=purchase_order,
                                       policy={**policy, 'po_check': po_check, 'reviewed': reviewed,
                                               'counterparty_role': 'customer' if receivable else 'supplier',
                                               'counterparty_name': values.get('customer') if receivable else values.get('supplier')})
    if po_error:
        result['errors'].append(po_error)
    reached = any(error['code'] in {'EX-003', 'EX-004', 'EX-005'} for error in result['errors']) or not result['errors']
    independent_errors, independent_checks = independent_arithmetic(invoice, items, fields, reached)
    result['errors'].extend(independent_errors); result['checks'].extend(independent_checks)
    if basis is not None:
        # Name what was actually compared; these remain genuine arithmetic failures.
        for error in result['errors']:
            if error['code'] in {'EX-004', 'EX-005'}:
                error['message'] = error['message'].replace('subtotal', 'printed tax-exclusive total')
                if error.get('field') == 'subtotal':
                    error['field'] = 'total_excluding_tax'
        result['checks'].append({'rule': PRETAX_BASIS_RULE, 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
            'message': 'No subtotal is printed. The printed tax-exclusive total was used as the pre-tax basis for arithmetic checks; subtotal stays empty.'})
    elif basis_reason:
        result['checks'].append({'rule': PRETAX_BASIS_RULE, 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': False,
                                 'message': basis_reason})
    if derived is not None:
        result['checks'].append({'rule': 'SUBTOTAL-DERIVED', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False, 'field': 'subtotal',
            'message': f'No subtotal is printed. The {len(items)} line amounts add up to {derived}, and the printed amounts then reach the total exactly. Subtotal stays empty.'})
    tax_check = tax_status_check(tax_state, raw, reconciles=reconciles, required=True, reviewed=reviewed.get('TAX-STATUS'))
    if tax_check:
        result['checks'].append(tax_check)
    if reconciles and not any(e['code'] == 'EX-005' for e in result['errors']):
        result['checks'].append({'rule': 'TOTAL-COMPONENTS', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                                 'message': f'Printed amounts reconcile: {equation["text"]}.'})
    errors.extend(error for error in result['errors'] if not (error['code'] == 'EX-001' and error['field'] in invalid))
    extended = evaluate_complex_invoice_financials(extraction, fields, items)
    errors.extend(extended['errors'])
    result['checks'].extend(extended['checks'])
    vendor = result['vendor_match']
    return {'errors': errors, 'checks': result['checks'],
            'vendor_match': {key: vendor.get(key) for key in ('status','vendor_id','vendor_name','similarity','score_method')},
            'transaction_anomaly': result.get('transaction_anomaly'),
            'review_required': True, 'accounting_approval_performed': False}


def evaluate_document(document, extraction, fields, items, db=None):
    """Route effective values by source-verified kind, preserving invoice rules."""
    raw = extraction.get('raw_json') or {}
    classification = raw.get('document_type') or {}
    is_m2 = raw.get('mode') == 'module2_public_result'
    kind = classification.get('value') if is_m2 else document['kind']
    if kind == 'invoice':
        result = evaluate_invoice(document, extraction, fields, items, db)
        values = _profile_values(fields)
        profile = validate_profile(kind, values, items)
        result['errors'].extend(profile['errors']); result['checks'].extend(profile['checks'])
        result['errors'].extend(model_disagreements(fields))
        result['errors'] = _with_causes(consolidate_errors(result['errors']))
        result['coverage_complete'] = coverage_complete(result['checks'])
        result['validation_passed'] = not result['errors']
        return result
    errors = []
    if kind not in {'bill', 'receipt', 'credit_note', 'debit_note'} or (is_m2 and (
            classification.get('status') != 'classified' or kind != document['kind'])):
        errors.append({'code': 'EX-002', 'field': 'document_type', 'severity': 'high',
                       'message': 'Source document kind is unknown, ambiguous or disagrees with intake; review the source.'})
    if not errors:
        result = evaluate_financial_document(kind, extraction, fields, items)
        values = _profile_values(fields)
        profile = validate_profile(kind, values, items)
        result['errors'].extend(profile['errors']); result['checks'].extend(profile['checks'])
        result['errors'].extend(model_disagreements(fields))
        result['errors'] = consolidate_errors(result['errors'])
        receivable = document.get('category') == 'accounts_receivable'
        role = 'customer' if receivable else 'supplier'
        catalogue = read_or_unavailable(db, lambda: customer_catalogue(db, document) if receivable else invoice_references(db, document)[0])
        name = values.get(role)
        if not catalogue or not name:
            vendor_match = {'status': 'NOT_CHECKED', 'vendor_id': None,
                            'vendor_name': None, 'confidence': None}
            unreadable = db is not None and catalogue is None and bool(name)
            result['checks'].append({'rule': 'EX-008', 'result': 'WARNING',
                'checkStatus': 'NOT_CHECKED', 'required': unreadable,
                'message': (f'The {role} list could not be read, so this name was not matched.' if unreadable else
                            f'No {role} name to match.' if not name else f'This company has no {role} list yet, so this name was not matched.')})
        else:
            vendor_match = find_vendor_match(name, catalogue)
            vendor_match['similarity'] = vendor_match.get('confidence')
            vendor_match['score_method'] = 'normalized_name_sequence_matcher'
            if vendor_match['status'] == 'ACCEPT':
                result['checks'].append({'rule': 'EX-008', 'result': 'PASS',
                    'checkStatus': 'CHECKED', 'required': False,
                    'message': f'Name matches the {role} list exactly; this does not approve the {role}.'})
            else:
                result['errors'].append({'code': 'EX-008', 'field': role, 'severity': 'high',
                    'message': f'{role.capitalize()} matching requires review.',
                    'vendor_candidate': {key: vendor_match.get(key) for key in
                        ('vendor_id', 'vendor_name', 'similarity', 'score_method')}})
        for rule, message in (('EX-007', 'Duplicate checks cover invoices today; bills and receipts are not in the policy.'),
                              ('EX-009', 'Purchase-order matching runs on invoices today.'),
                              ('EX-010', 'The anomaly check compares invoices only.')):
            result['checks'].append({'rule': rule, 'result': NOT_APPLICABLE_MARK, 'checkStatus': NOT_APPLICABLE_MARK,
                                     'required': False, 'message': message})
        result['coverage_complete'] = coverage_complete(result['checks'])
        result['validation_passed'] = not result['errors']
        result['vendor_match'] = {key: vendor_match.get(key) for key in
            ('status', 'vendor_id', 'vendor_name', 'similarity', 'score_method')}
        _with_causes(result['errors'])
        return result
    # No invoice number/vendor/line requirements are imposed on receipts/bills.
    # Parsing a total is not proof that all arithmetic or required-field rules passed.
    checks = [{'rule': f'{str(kind).upper()}_{scope}', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
               'message': f'{kind} {label} policy is not implemented; human review required.'}
              for scope, label in [('REQUIRED_FIELDS', 'required-field'), ('FINANCIAL_MATH', 'financial validation')]]
    return {'errors': _with_causes(errors), 'coverage_complete': False, 'checks': checks,
            'review_required': True, 'accounting_approval_performed': False}


def load_current(db, document_id, user=None, *, lock=False):
    params = [document_id]
    scope = ''
    if user is not None:
        scope += ' AND d.tenant_id=%s'
        params.append(user['tenant_id'])
        if user['role'] == 'accountant':
            scope += ' AND (d.owner_id=%s OR d.assigned_to=%s)'
            params.extend([user['id'], user['id']])
    document = db.execute('SELECT d.* FROM documents d WHERE d.id=%s' + scope + (' FOR UPDATE' if lock else ''), params).fetchone()
    if not document:
        raise HTTPException(404, 'Document not found.')
    extraction = db.execute('SELECT * FROM raw_extractions WHERE document_id=%s AND is_current=true', (document_id,)).fetchone()
    if not extraction:
        raise HTTPException(422, 'Document has no current extraction to validate.')
    fields = db.execute('SELECT * FROM field_predictions WHERE document_id=%s AND extraction_id=%s', (document_id, extraction['id'])).fetchall()
    items = db.execute('SELECT * FROM line_items WHERE document_id=%s AND extraction_id=%s ORDER BY line_number', (document_id, extraction['id'])).fetchall()
    return document, extraction, fields, items


def validation_outcome(results, errors, external, coverage_complete):
    """The one status calculation behind both POST /revalidate and GET /validation."""
    blocking = any(r.get('result') in {'FAIL', 'FAILED'} or r.get('preventsApproval') is True for r in results)
    return ('REVIEW_REQUIRED' if errors or external or blocking or not coverage_complete else 'PASS'), blocking


def stored_status(check):
    """(status, expected_value, prevents_approval, requires_review) for one check.

    The schema allows passed, failed, not_checked and warning. Until Aleeya approves a
    not_applicable value, it is stored as a non-blocking warning marked in expected_value,
    and read_status() turns it back. Nothing else stores a warning with that mark.
    """
    state = check.get('checkStatus')
    if state == NOT_APPLICABLE_MARK:
        return 'warning', NOT_APPLICABLE_MARK, False, False
    status = 'passed' if state == 'CHECKED' and check.get('result') == 'PASS' else ('not_checked' if state == 'NOT_CHECKED' else 'warning')
    blocks = status != 'passed' and check.get('required', True)
    return status, check.get('expected_value'), blocks, blocks


def read_status(row):
    """The result an API or screen should show for a stored row, in the four states."""
    if row.get('status') == 'warning' and row.get('expected_value') == NOT_APPLICABLE_MARK:
        return NOT_APPLICABLE_MARK
    return (row.get('status') or '').upper()


def validate_document(db, document_id, user=None):
    document, extraction, fields, items = load_current(db, document_id, user, lock=True)
    if document['status'] in {'completed', 'standardised'}:
        raise HTTPException(409, 'Approved records require an amendment.')
    if document['status'] in {'in_review', 'validated'}:
        raise HTTPException(409, 'This revision has already been submitted or approved. Save a correction before revalidating.')
    result = evaluate_document(document, extraction, fields, items, db)
    rule_version = RULE_VERSION
    db.execute("""UPDATE exceptions SET status='resolved',resolved_at=now(), note=COALESCE(note,'') || %s
        WHERE document_id=%s AND tenant_id=%s AND type LIKE %s AND status <> 'resolved'""",
        ('\nSuperseded by deterministic validation.', document_id, document['tenant_id'], 'VALIDATION:%'))
    for error in result['errors']:
        exception_type = 'VALIDATION:' + error['code']
        exception_message = error['field'] + ': ' + error['message'] + (' (also required by ' + ', '.join(error['relatedRules']) + ')' if error.get('relatedRules') else '')
        db.execute("""INSERT INTO exceptions(document_id,tenant_id,type,severity,message)
            SELECT %s,%s,%s,%s,%s
            WHERE NOT EXISTS (
              SELECT 1 FROM exceptions
              WHERE document_id=%s AND tenant_id=%s AND type=%s AND status <> 'resolved'
            )""", (document_id, document['tenant_id'], exception_type,
                error.get('severity', EXCEPTION_SEVERITY.get(error['code'], 'high')), exception_message,
                document_id, document['tenant_id'], exception_type))
    # Replace only this revision's structured results. Older revisions remain
    # available for audit and cannot satisfy the current standardisation gate.
    db.execute('DELETE FROM validation_results WHERE document_id=%s AND content_revision=%s',
               (document_id, document['content_revision']))
    for check in result.get('checks', []):
        status, expected, blocks, requires_review = stored_status(check)
        db.execute("""INSERT INTO validation_results
            (document_id,tenant_id,content_revision,rule_id,rule_source,rule_version,
             document_type,status,severity,field_key,actual_value,expected_value,difference,prevents_approval,requires_review,message)
            VALUES (%s,%s,%s,%s,'SAIC',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (document_id, document['tenant_id'], document['content_revision'], check.get('rule', 'UNKNOWN'),
             rule_version, document['kind'], status, 'info' if status == 'passed' or expected == NOT_APPLICABLE_MARK else 'medium',
             check.get('field'), check.get('actual_value'), expected, check.get('difference'), blocks, requires_review, check.get('message', '')))
    for error in result.get('errors', []):
        severity = error.get('severity', 'high')
        db.execute("""INSERT INTO validation_results
            (document_id,tenant_id,content_revision,rule_id,rule_source,rule_version,
             document_type,status,severity,field_key,actual_value,expected_value,difference,prevents_approval,requires_review,message)
            VALUES (%s,%s,%s,%s,'SAIC',%s,%s,'failed',%s,%s,%s,%s,%s,TRUE,TRUE,%s)""",
            (document_id, document['tenant_id'], document['content_revision'], error.get('code', 'UNKNOWN'),
             rule_version, document['kind'], severity, error.get('field'), error.get('actual_value'), error.get('expected_value'),
             error.get('difference'), error.get('message', '')))
    blocking = db.execute("""SELECT 1 FROM exceptions WHERE document_id=%s AND tenant_id=%s
        AND status <> 'resolved' AND severity='high' LIMIT 1""", (document_id, document['tenant_id'])).fetchone()
    status = 'exception' if blocking or result['errors'] else 'extracted'
    db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
               (str(user['id']) if user else '', 'Document validation; human approval still required'))
    ownership, owner_params = '', []
    if user is not None and user['role'] == 'accountant':
        ownership = ' AND (owner_id=%s OR assigned_to=%s)'
        owner_params = [user['id'], user['id']]
    updated = db.execute("""UPDATE documents SET status=%s,updated_at=now()
        WHERE id=%s AND tenant_id=%s AND content_revision=%s""" + ownership + ' RETURNING status',
        [status, document_id, document['tenant_id'], document['content_revision'], *owner_params]).fetchone()
    if not updated:
        raise HTTPException(409, 'Document changed while validating.')
    stored = db.execute("""SELECT status,prevents_approval FROM validation_results
        WHERE document_id=%s AND tenant_id=%s AND content_revision=%s""",
        (document_id, document['tenant_id'], document['content_revision'])).fetchall() or []
    open_exceptions = db.execute("""SELECT type,severity FROM exceptions
        WHERE document_id=%s AND tenant_id=%s AND status <> 'resolved'""",
        (document_id, document['tenant_id'])).fetchall() or []
    external = [row for row in open_exceptions if not row['type'].startswith('VALIDATION:')]
    results = [{'result': row['status'].upper(), 'preventsApproval': row['prevents_approval']} for row in stored]
    results += [{'result': 'FAIL' if row['severity'] == 'high' else 'WARNING'} for row in external]
    validation_status, has_blockers = validation_outcome(results, result['errors'], external, result['coverage_complete'])
    return {'documentId': str(document_id), 'contentRevision': document['content_revision'], 'status': status.upper(),
            'validationStatus': validation_status, 'blocking': has_blockers,
            'reviewRequired': True, 'accountingApprovalPerformed': False, 'errors': result['errors'],
            'coverageComplete': result['coverage_complete'], 'validationPassed': result.get('validation_passed', not result['errors']),
            'checks': result['checks']}
