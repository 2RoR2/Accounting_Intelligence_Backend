"""Offline current-value rule contracts; PostgreSQL/API tests are separate."""
from copy import deepcopy
from decimal import Decimal
import unittest

from validation.document_validation import evaluate_document


def prediction(key, value):
    return {'field_key': key, 'current_value': value, 'normalized_value': value,
            'raw_value': value, 'extraction_state': 'resolved' if value is not None else 'missing',
            'resolution': 'parser_preserved', 'evidence': []}


class BillReceiptValidationTests(unittest.TestCase):
    def setUp(self):
        self.fields = [prediction(key, value) for key, value in {
            'supplier':'Synthetic Retail', 'date':'2026-10-19', 'currency':'MYR',
            'subtotal':'10.00', 'tax':'0.60', 'total':'10.60'}.items()]
        self.items = [{'description':'Synthetic item', 'quantity':Decimal('2'),
                       'unit_price':Decimal('5.00'), 'amount':Decimal('10.00')}]

    def evaluate(self, kind='receipt', classification=None, **raw):
        return evaluate_document({'kind':kind}, {'raw_json': {'mode':'module2_public_result',
            'document_type': classification or {'value':kind, 'status':'classified'}, **raw}},
            self.fields, self.items)

    def set_field(self, key, value):
        self.fields = [row for row in self.fields if row['field_key'] != key] + [prediction(key, value)]

    def test_receipt_complete_simple_layout_checks_math_without_invoice_fields(self):
        result = self.evaluate()
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['coverage_complete'])
        self.assertTrue(result['review_required'])
        self.assertFalse(result['accounting_approval_performed'])
        self.assertEqual({c['rule'] for c in result['checks']},
                         {'RECEIPT-FIELDS','RECEIPT-LINE_DETAILS','RECEIPT-LINE_MATH','RECEIPT-SUBTOTAL','RECEIPT-TOTAL'})

    def test_bill_due_is_not_assumed_equal_to_original_charges(self):
        self.set_field('reference', 'SYN-BILL')
        self.set_field('total', '80.00')
        result = self.evaluate('bill')
        self.assertEqual(result['errors'], [])
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(any(c['rule'] == 'BILL-AMOUNT_DUE' and c['checkStatus'] == 'NOT_CHECKED' for c in result['checks']))

    def test_bill_account_identifier_can_replace_bill_reference(self):
        self.set_field('account_number', 'SYN-ACCOUNT')
        self.assertEqual(self.evaluate('bill')['errors'], [])

    def test_receipt_reference_payment_method_time_and_customer_optional(self):
        self.set_field('reference', None)
        self.set_field('transaction_time', None)
        self.set_field('payment_method', None)
        self.assertTrue(self.evaluate()['coverage_complete'])

    def test_missing_merchant_date_currency_total_remain_unresolved(self):
        for key in ('supplier', 'date', 'currency', 'total'):
            with self.subTest(key=key):
                saved = deepcopy(self.fields)
                self.set_field(key, None)
                result = self.evaluate()
                self.assertFalse(result['coverage_complete'])
                self.assertTrue(any(e['code'] == 'RECEIPT-REQUIRED' and e['field'] == key for e in result['errors']))
                self.assertIsNone(self.fields[-1]['current_value'])
                self.fields = saved

    def test_missing_tax_is_not_zero_and_total_check_not_executed(self):
        self.set_field('tax', None)
        result = self.evaluate()
        self.assertEqual(result['errors'], [])
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(next(c for c in result['checks'] if c['rule'] == 'RECEIPT-TOTAL')['checkStatus'], 'NOT_CHECKED')

    def test_explicit_zero_is_valid(self):
        for key in ('subtotal','tax','total'):
            self.set_field(key, '0.00')
        self.items[0].update(unit_price=Decimal('0'), amount=Decimal('0'))
        self.assertTrue(self.evaluate()['coverage_complete'])

    def test_all_three_receipt_math_failures_are_independent_high_severity(self):
        self.items[0]['amount'] = Decimal('9.00')
        self.set_field('total','99.00')
        errors = self.evaluate()['errors']
        self.assertEqual({e['code'] for e in errors}, {'RECEIPT-LINE_MATH','RECEIPT-SUBTOTAL','RECEIPT-TOTAL'})
        self.assertTrue(all(e['severity'] == 'high' for e in errors))

    def test_bill_independent_line_and_subtotal_math(self):
        self.set_field('reference', 'SYN-B')
        self.items[0]['amount'] = Decimal('9.00')
        self.assertEqual({e['code'] for e in self.evaluate('bill')['errors']}, {'BILL-LINE_MATH','BILL-SUBTOTAL'})

    def test_bill_two_column_charge_not_forced_to_quantity_one(self):
        self.set_field('reference','SYN-B')
        self.set_field('subtotal',None)
        self.set_field('tax',None)
        self.items[0].update(quantity=None, unit_price=None)
        before = deepcopy((self.fields,self.items))
        result = self.evaluate('bill')
        self.assertEqual(result['errors'], [])
        self.assertFalse(result['coverage_complete'])
        self.assertEqual((self.fields,self.items), before)

    def test_bill_due_and_period_order_findings(self):
        self.set_field('reference','SYN-B')
        self.set_field('due','2026-10-01')
        self.set_field('billing_period','2026-09-30 to 2026-09-01')
        result = self.evaluate('bill')
        self.assertEqual({e['field'] for e in result['errors']}, {'due','billing_period'})
        self.assertTrue(all(e['code'] == 'BILL-DATES' and e['severity'] == 'medium' for e in result['errors']))

    def test_bill_invalid_calendar_and_unsupported_period_are_distinct(self):
        self.set_field('reference','SYN-B')
        self.set_field('billing_period','2026-02-30 to 2026-03-01')
        self.assertTrue(any(e['code'] == 'BILL-FORMAT' for e in self.evaluate('bill')['errors']))
        self.set_field('billing_period','September 2026')
        result = self.evaluate('bill')
        self.assertEqual(result['errors'], [])
        self.assertTrue(any(c['rule'] == 'BILL-BILLING_PERIOD' and c['checkStatus'] == 'NOT_CHECKED' for c in result['checks']))

    def test_receipt_bad_time_date_currency_and_float_are_not_coerced(self):
        for key,value in [('transaction_time','25:00'),('date','2026-02-30'),('currency','RM'),('total',10.6)]:
            with self.subTest(key=key):
                saved=deepcopy(self.fields)
                self.set_field(key,value)
                self.assertTrue(any(e['code'] == 'RECEIPT-FORMAT' and e['field'] == key for e in self.evaluate()['errors']))
                self.fields=saved

    def test_unresolved_candidate_never_validates_as_value(self):
        self.fields[-1].update(current_value=None, normalized_value=None, extraction_state='ambiguous',
                               resolution='candidate_for_review', candidates=[{'value':'10.60'}])
        self.assertTrue(any(e['code'] == 'RECEIPT-FORMAT' for e in self.evaluate()['errors']))

    def test_saved_correction_overrides_raw_candidate_without_mutation(self):
        self.fields[-1].update(raw_value='99.00', normalized_value=None, extraction_state='ambiguous',
                               resolution='candidate_for_review', corrected_by='reviewer', corrected_at='2026-10-19')
        before=deepcopy(self.fields)
        self.assertTrue(self.evaluate()['coverage_complete'])
        self.assertEqual(self.fields,before)

    def test_null_lines_and_malformed_line_values_do_not_pass(self):
        for value in (None, 5.0, 'NaN', True):
            with self.subTest(value=value):
                self.items[0]['unit_price']=value
                self.assertFalse(self.evaluate()['coverage_complete'])

    def test_bill_negative_balance_not_forced_to_invoice_positive_rule(self):
        self.set_field('reference','SYN-B')
        self.set_field('total','-80.00')
        self.assertEqual(self.evaluate('bill')['errors'], [])
        self.assertFalse(self.evaluate('bill')['coverage_complete'])

    def test_receipt_negative_return_not_supported(self):
        self.set_field('total','-10.60')
        self.assertFalse(self.evaluate()['coverage_complete'])
        self.assertTrue(any(c['rule'] == 'RECEIPT-RETURNS' and c['checkStatus'] == 'NOT_CHECKED' for c in self.evaluate()['checks']))

    def test_receipt_missing_line_description_is_not_complete(self):
        self.items[0]['description']=None
        self.assertFalse(self.evaluate()['coverage_complete'])

    def test_consistent_signed_return_is_not_falsely_passed_or_declared_malformed(self):
        self.set_field('subtotal','-10.00')
        self.set_field('tax','-0.60')
        self.set_field('total','-10.60')
        self.items[0].update(quantity=Decimal('-2'),amount=Decimal('-10.00'))
        result=self.evaluate()
        self.assertEqual(result['errors'],[])
        self.assertFalse(result['coverage_complete'])

    def test_unverified_or_conflicting_classification_cannot_route_to_receipt(self):
        for classification in ({'value':'receipt','status':'ambiguous'}, {'value':'unknown','status':'unknown'},
                               {'value':'bill','status':'classified'}):
            self.assertFalse(self.evaluate(classification=classification)['coverage_complete'])
            self.assertTrue(self.evaluate(classification=classification)['errors'])

    def test_explicit_unmapped_adjustment_or_table_defers_total_check(self):
        for text in ('Discount: 1.00','Tip: 2.00'):
            self.fields[0]['evidence']=[{'text':text}]
            self.assertFalse(self.evaluate()['coverage_complete'])
        self.fields[0]['evidence']=[]
        self.assertFalse(self.evaluate(issues=[{'code':'UNSUPPORTED_TABLE'}])['coverage_complete'])

    def test_decimal_tolerance_and_fractional_quantity(self):
        self.items[0].update(quantity=Decimal('1.125'),unit_price=Decimal('8.8889'),amount=Decimal('10.00'))
        self.assertTrue(self.evaluate()['coverage_complete'])

    def test_duplicate_alias_does_not_choose_one(self):
        self.fields.append(prediction('merchant','Other'))
        self.assertTrue(any(e['code'] == 'RECEIPT-FORMAT' for e in self.evaluate()['errors']))


if __name__ == '__main__':
    unittest.main()
