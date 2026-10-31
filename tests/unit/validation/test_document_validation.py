"""Offline contracts for review-required Decimal validation of persisted M2 data."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from services.extraction.contracts.accounting_contract import map_m2_result
from validation.documents.document_validation import evaluate_invoice
from validation.documents.models import Invoice, finite_decimal

FIXTURE = Path(__file__).parents[2]/'extraction/data/contracts/syn001_m2.json'


class DocumentValidationTests(unittest.TestCase):
    def setUp(self):
        self.result = json.loads(FIXTURE.read_text())
        self.doc = {'kind': 'invoice', 'category': 'accounts_payable'}

    def evaluate(self):
        mapped = map_m2_result(self.result, self.result['document_id'])
        return evaluate_invoice(self.doc, {'raw_json': self.result}, mapped['fields'], mapped['line_items'])

    def test_complete_invoice_is_valid_but_still_requires_human_review(self):
        result = self.evaluate()
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['review_required'])
        self.assertFalse(result['accounting_approval_performed'])

    def test_total_mismatch_is_blocking_and_original_is_unchanged(self):
        self.result['fields']['total_amount']['value'] = '99.00'
        before = deepcopy(self.result)
        result = self.evaluate()
        self.assertTrue(any(error['code'] == 'EX-005' for error in result['errors']))
        self.assertEqual(self.result, before)

    def test_approved_reference_history_flows_into_validation_exception(self):
        mapped = map_m2_result(self.result, self.result['document_id'])
        history = [
            {'transaction_id': f'H{i}', 'amount': '0.01', 'currency': 'MYR'}
            for i in range(1, 6)
        ]
        self.doc.update(id='current-document', tenant_id='tenant-1')
        with patch('validation.documents.document_validation.invoice_references', return_value=(None, [])), \
             patch('validation.documents.document_validation.transaction_references', return_value=history):
            result = evaluate_invoice(self.doc, {'raw_json': self.result}, mapped['fields'],
                                      mapped['line_items'], db=object())

        self.assertEqual(result['transaction_anomaly']['status'], 'anomaly')
        self.assertTrue(any(error['code'] == 'EX-010' for error in result['errors']))

    def test_missing_currency_and_invoice_number_are_not_invented(self):
        for name in ('currency', 'invoice_number'):
            self.result['fields'][name].update(value=None, state='missing', resolution='unresolved')
        result = self.evaluate()
        self.assertEqual({error['field'] for error in result['errors']}, {'currency', 'invoice_number'})

    def test_fractional_quantity_stays_decimal_through_existing_model(self):
        invoice = Invoice(invoice_number='SYN-F', vendor_name='Synthetic', invoice_date='2026-10-19',
                          currency='MYR', subtotal='28.26', tax_amount='0.00', total_amount='28.26',
                          line_items=[{'description':'Synthetic', 'quantity':'1.125', 'unit_price':'25.1200', 'amount':'28.26'}])
        self.assertIsInstance(invoice.line_items[0].quantity, Decimal)
        self.assertEqual(invoice.line_items[0].quantity * invoice.line_items[0].unit_price, Decimal('28.2600000'))

    def test_explicit_zero_values_are_valid(self):
        for name in ('subtotal', 'tax_amount', 'total_amount'):
            self.result['fields'][name]['value'] = '0.00'
        for name in ('unit_price', 'line_total'):
            self.result['line_items'][0][name]['value'] = '0.00'
        self.assertEqual(self.evaluate()['errors'], [])

    def test_ai_candidate_is_not_promoted(self):
        self.result['fields']['invoice_number'].update(value=None, state='missing', resolution='candidate_for_review', ai_candidate='AI-GUESS')
        self.assertTrue(any(error['field'] == 'invoice_number' for error in self.evaluate()['errors']))

    def test_unclassified_invoice_is_not_accepted(self):
        self.result['document_type'].update(value='unknown', status='unknown')
        self.assertTrue(any(error['field'] == 'document_type' for error in self.evaluate()['errors']))

    def test_line_arithmetic_mismatch_is_blocking(self):
        self.result['line_items'][0]['line_total']['value'] = '49.00'
        self.assertTrue(any(error['code'] == 'EX-003' for error in self.evaluate()['errors']))

    def test_unknown_business_direction_is_not_defaulted(self):
        self.doc['category'] = None
        self.assertTrue(any(error['field'] == 'category' for error in self.evaluate()['errors']))

    def test_human_current_value_takes_precedence(self):
        mapped = map_m2_result(self.result, self.result['document_id'])
        for field in mapped['fields']:
            if field['field_key'] == 'total':
                field.update(current_value='99.00', corrected_by='reviewer', corrected_at='2026-10-19')
        result = evaluate_invoice(self.doc, {'raw_json': self.result}, mapped['fields'], mapped['line_items'])
        self.assertTrue(any(error['code'] == 'EX-005' for error in result['errors']))

    def test_float_nonfinite_and_malformed_decimals_are_rejected(self):
        for value in (0.1, True, 'NaN', Decimal('Infinity'), '12,34.50', '1e3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finite_decimal(value)

    def test_discount_math_and_null_discount_are_distinct(self):
        payload = {'invoice_number':'SYN-D', 'vendor_name':'Synthetic', 'invoice_date':'2026-10-19',
                   'currency':'MYR', 'subtotal':'50.00', 'tax_amount':'0.00', 'total_amount':'45.00',
                   'discount':'5.00', 'line_items':[{'description':'Synthetic', 'quantity':'2', 'unit_price':'25.00', 'amount':'50.00'}]}
        self.assertEqual(Invoice(**payload).total_amount, Decimal('45.00'))
        payload.update(discount=None, total_amount='50.00')
        self.assertIsNone(Invoice(**payload).discount)
        payload['total_amount'] = '45.00'
        with self.assertRaises(ValueError):
            Invoice(**payload)


if __name__ == '__main__':
    unittest.main()
