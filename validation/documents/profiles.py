"""Document-profile rules for conditional accounting validation.

Profiles are deliberately conservative: absent optional evidence is NOT_CHECKED,
while an evidenced but invalid value is a validation failure.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, time
import re

PROFILES = {
    'invoice': {'required': {'supplier', 'date', 'currency', 'total'}, 'optional': {
        'invoice', 'due', 'payment_terms', 'allowances', 'charges', 'prepaid', 'rounding', 'amount_due',
        'expense_category', 'payment_reference', 'received_date', 'tax_breakdown'}},
    'receipt': {'required': {'supplier', 'date', 'currency', 'total'}, 'optional': {
        'reference', 'transaction_time', 'payment_method', 'tendered_amount', 'change_given',
        'payment_reference', 'tax_breakdown', 'refund_indicator'}},
    'bill': {'required': {'supplier', 'date', 'currency', 'total'}, 'optional': {
        'reference', 'received_date', 'due', 'payment_terms', 'purchase_order_number',
        'goods_receipt_reference', 'expense_category', 'amount_paid', 'outstanding_amount',
        'payment_reference', 'tax_breakdown'}},
    'credit_note': {'required': {'supplier', 'reference', 'date', 'currency', 'total'}, 'optional': {
        'invoice', 'payment_reference', 'tax_breakdown'}},
    'debit_note': {'required': {'supplier', 'reference', 'date', 'currency', 'total'}, 'optional': {
        'invoice', 'payment_reference', 'tax_breakdown'}},
}

def _money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def validate_profile(kind, values, items, *, supporting=None):
    values = values or {}
    supporting = supporting or {}
    errors, checks = [], []
    def issue(rule, field, message, severity='high'):
        errors.append({'code': rule, 'field': field, 'message': message, 'severity': severity})
    def check(rule, message, available=True, required=False):
        checks.append({'rule': rule, 'result': 'PASS' if available else 'WARNING',
                       'checkStatus': 'CHECKED' if available else 'NOT_CHECKED',
                       'required': required, 'message': message})
    profile = PROFILES.get(kind)
    if not profile:
        issue('PROFILE-001', 'document_type', 'No validation profile is configured.')
        return {'errors': errors, 'checks': checks, 'coverage_complete': False}
    required_fields = profile['required']
    if kind == 'bill' and values.get('total') is None and values.get('amount_due') is not None:
        required_fields = (required_fields - {'total'}) | {'amount_due'}
    for field in required_fields:
        if values.get(field) in (None, ''):
            issue('PROFILE-REQUIRED', field, f'{field} is required for {kind}.')
    if values.get('transaction_time') is not None:
        try: time.fromisoformat(str(values['transaction_time']))
        except ValueError: issue('RCP-TRANSACTION-TIME', 'transaction_time', 'Invalid local time.')
    if values.get('received_date') is not None:
        try: date.fromisoformat(str(values['received_date']))
        except ValueError: issue('AP-RECEIVED-DATE', 'received_date', 'Invalid received date.')
    if values.get('payment_method') is not None and str(values['payment_method']).lower() not in {
        'cash', 'card', 'bank_transfer', 'cheque', 'e_wallet', 'direct_debit', 'other'}:
        issue('RCP-PAYMENT-METHOD', 'payment_method', 'Unsupported payment method.')
    if values.get('tendered_amount') is not None and values.get('change_given') is not None and values.get('total') is not None:
        expected = _money(values['tendered_amount']) - _money(values['change_given'])
        if abs(expected - _money(values['total'])) > Decimal('0.01'):
            issue('RCP-005', 'change_given', f'Cash tender less change should equal {expected}.', 'medium')
    elif kind == 'receipt':
        check('RCP-005', 'Cash tender/change not supplied.', available=False)
    if kind in {'credit_note', 'debit_note'}:
        if not (values.get('invoice') or supporting.get('related_invoice')):
            issue('NOTE-LINK', 'invoice', 'Credit/debit note must link to an original invoice.', 'high')
    if kind == 'receipt' and values.get('refund_indicator') in (True, 'true', 'TRUE', 'refund'):
        check('RCP-007', 'Refund treatment required; ordinary expense treatment is blocked.', available=False, required=True)
    if values.get('amount_paid') is not None and values.get('outstanding_amount') is not None and values.get('total') is not None:
        expected = _money(values['total']) - _money(values['amount_paid'])
        if abs(expected - _money(values['outstanding_amount'])) > Decimal('0.01'):
            issue('AP-009', 'outstanding_amount', f'Outstanding amount should equal {expected}.')
    elif kind == 'bill':
        check('AP-009', 'Payment/outstanding evidence not supplied.', available=False)
    return {'errors': errors, 'checks': checks,
            'coverage_complete': not errors and all(c['checkStatus'] == 'CHECKED' for c in checks if c.get('required'))}
