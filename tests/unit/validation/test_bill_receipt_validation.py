"""Offline current-value rule contracts; PostgreSQL/API tests are separate."""
from copy import deepcopy
from decimal import Decimal
import unittest

from validation.documents.document_validation import evaluate_document


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
        self.assertEqual({c['rule'] for c in result['checks'] if c.get('required', True)},
                         {'RECEIPT-FIELDS','RECEIPT-LINE_DETAILS','RECEIPT-LINE_MATH','RECEIPT-SUBTOTAL','RECEIPT-TOTAL'})
        # Supplier matching and cash-tender checks were added later as optional: reported, never passed silently.
        optional = {c['rule']: c['checkStatus'] for c in result['checks'] if not c.get('required', True)}
        self.assertEqual(optional, {'EX-008': 'NOT_CHECKED', 'RCP-005': 'NOT_CHECKED', 'TAX-STATUS': 'CHECKED', 'EX-007': 'NOT_APPLICABLE',
                                    'EX-009': 'NOT_APPLICABLE', 'EX-010': 'NOT_APPLICABLE'})

    def test_bill_due_is_not_assumed_equal_to_original_charges(self):
        self.set_field('reference', 'SYN-BILL')
        self.set_field('total', '80.00')
        result = self.evaluate('bill')
        self.assertEqual(result['errors'], [])
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(any(c['rule'] == 'BILL-AMOUNT_DUE' and c['checkStatus'] == 'NOT_CHECKED' for c in result['checks']))

    def amount_due_check(self):
        self.set_field('reference', 'SYN-BILL')
        return next(c for c in self.evaluate('bill')['checks'] if c['rule'] == 'BILL-AMOUNT_DUE')

    def test_printed_due_equal_to_total_passes_the_check(self):
        self.set_field('total', '80.00'); self.set_field('amount_due', '80.00')
        check = self.amount_due_check()
        self.assertEqual((check['checkStatus'], check['result']), ('CHECKED', 'PASS'))

    def test_printed_due_that_differs_from_total_stays_unchecked_and_required(self):
        self.set_field('total', '80.00'); self.set_field('amount_due', '95.00')
        check = self.amount_due_check()
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))

    def test_printed_payment_keeps_the_amount_due_check_blocking(self):
        self.set_field('total', '80.00'); self.set_field('amount_due', '80.00'); self.set_field('amount_paid', '10.00')
        check = self.amount_due_check()
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))

    def receipt_checks(self, lines, **fields):
        self.fields = [row for row in self.fields if row['field_key'] not in ('subtotal', 'tax')]
        for key, value in fields.items():
            self.set_field(key, value)
        self.items = lines
        result = self.evaluate('receipt')
        return result, {c['rule']: c for c in result['checks']}

    def line(self, amount, quantity=None, unit_price=None):
        return {'description': 'Item', 'quantity': quantity, 'unit_price': unit_price, 'amount': amount, 'field_states': {}}

    def test_printed_item_totals_do_not_need_a_unit_price(self):
        result, checks = self.receipt_checks([self.line('6.60'), self.line('4.00')], total='10.60')
        self.assertEqual(result['errors'], [])
        self.assertEqual((checks['RECEIPT-LINE_MATH']['checkStatus'], checks['RECEIPT-LINE_MATH']['required']), ('NOT_APPLICABLE', False))
        self.assertEqual(checks['RECEIPT-SUBTOTAL']['checkStatus'], 'CHECKED')
        self.assertEqual(checks['RECEIPT-TOTAL']['checkStatus'], 'CHECKED')
        self.assertTrue(result['coverage_complete'])

    def test_a_printed_price_that_disagrees_still_fails(self):
        result, _ = self.receipt_checks([self.line('10.60', '2', '9.00')], total='10.60')
        self.assertIn('RECEIPT-LINE_MATH', {e['code'] for e in result['errors']})

    def test_items_that_do_not_reach_the_total_stay_blocked(self):
        result, checks = self.receipt_checks([self.line('6.60'), self.line('1.00')], total='10.60')
        self.assertEqual(checks['RECEIPT-SUBTOTAL']['checkStatus'], 'NOT_CHECKED')
        self.assertEqual(checks['RECEIPT-TOTAL']['checkStatus'], 'NOT_CHECKED')
        self.assertFalse(result['coverage_complete'])

    def test_a_missing_item_amount_stays_blocked(self):
        result, checks = self.receipt_checks([self.line('10.60'), self.line(None)], total='10.60')
        self.assertEqual((checks['RECEIPT-LINE_DETAILS']['checkStatus'], checks['RECEIPT-LINE_DETAILS']['required']), ('NOT_CHECKED', True))
        self.assertFalse(result['coverage_complete'])

    def test_standardising_a_receipt_needs_description_and_amount_only(self):
        from services.extraction.contracts.accounting_contract import approved_snapshot
        document = {'kind': 'receipt', 'category': 'accounts_payable', 'content_revision': 1}
        extraction = {'raw_json': {'mode': 'module2_public_result', 'document_type': {'value': 'receipt', 'status': 'classified'}}}
        row = {'id': 'l1', 'line_number': 1, 'description': 'Item', 'quantity': None, 'unit_price': None, 'amount': Decimal('10.60')}
        self.fields = [{'field_label': f['field_key'], 'confidence': None, 'candidates': [], 'corrected_by': None, 'corrected_at': None, **f} for f in self.fields]
        self.assertIsNone(approved_snapshot(document, extraction, self.fields, [row])['lineItems'][0]['unit_price'])
        with self.assertRaisesRegex(ValueError, 'Line items'):
            approved_snapshot(document, extraction, self.fields, [{**row, 'amount': None}])
        with self.assertRaisesRegex(ValueError, 'Line items'):
            approved_snapshot({**document, 'kind': 'invoice'}, {'raw_json': {'mode': 'module2_public_result', 'document_type': {'value': 'invoice', 'status': 'classified'}}}, self.fields, [row])

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
                # A required value that fails is a failure, kept apart from a rule that never ran.
                self.assertFalse(result['validation_passed'])
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
        for value in (5.0, 'NaN', True):
            with self.subTest(value=value):
                self.items[0]['unit_price']=value
                self.assertFalse(self.evaluate()['validation_passed'])
        self.items[0].update(unit_price=None, field_states={'unit_price': 'ambiguous'})
        self.assertFalse(self.evaluate()['validation_passed'], 'An unreadable printed price must not pass')
        self.items[0].update(amount=None, unit_price=Decimal('5.00'), field_states={})
        self.assertFalse(self.evaluate()['coverage_complete'], 'A missing line amount must not pass')

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


class PrintedComponentReconciliationTests(unittest.TestCase):
    """Service charge, rounding and absent tax, checked from printed values in exact decimals."""

    @staticmethod
    def field(key, value, state='resolved', candidates=()):
        return {'field_key': key, 'extraction_state': state if value is not None else 'missing', 'normalized_value': value,
                'current_value': value, 'resolution': 'parser_preserved', 'candidates': list(candidates), 'evidence': []}

    def invoice(self, *, subtotal='18.20', charges='1.82', rounding='-0.02', total='20.00', tax=None, tax_candidates=()):
        from validation.documents.document_validation import evaluate_invoice
        fields = [self.field('supplier', 'SL SOFTWARE SOLUTIONS SDN. BHD.'), self.field('invoice', 'T01-000008'), self.field('date', '2023-12-29'),
                  self.field('currency', 'MYR'), self.field('subtotal', subtotal), self.field('charges', charges), self.field('rounding', rounding),
                  self.field('total', total), {**self.field('tax', tax), 'candidates': list(tax_candidates)}]
        items = [{'description': 'Chicken Floss Toast', 'quantity': '1.00', 'unit_price': '9.30', 'amount': '9.30', 'field_states': {}},
                 {'description': 'Earl Grey', 'quantity': '1.00', 'unit_price': '8.90', 'amount': '8.90', 'field_states': {}}]
        return evaluate_invoice({'id': None, 'kind': 'invoice', 'category': 'accounts_payable'}, {'raw_json': {}}, fields, items)

    def test_service_charge_and_rounding_reconcile_with_no_tax_invented(self):
        result = self.invoice()
        self.assertEqual([e['code'] for e in result['errors']], [])
        total = next(c for c in result['checks'] if c['rule'] == 'TOTAL-COMPONENTS')
        self.assertIn('18.20 + 1.82 charges - 0.02 rounding = 20.00', total['message'])
        tax = next(c for c in result['checks'] if c['rule'] == 'TAX-STATUS')
        # The amounts adding up does not prove no tax was charged: prices may include it. A person confirms.
        self.assertEqual((tax['checkStatus'], tax['required']), ('NOT_CHECKED', True))
        self.assertIn('Tax stays empty', tax['message'])

    def test_a_total_that_does_not_add_up_keeps_tax_required(self):
        result = self.invoice(total='21.00')
        self.assertIn('EX-001', [e['code'] for e in result['errors']])
        self.assertFalse(any(c['rule'] == 'TAX-STATUS' for c in result['checks']))

    def test_one_cent_inside_the_tolerance_is_not_proof_there_is_no_tax(self):
        result = self.invoice(total='20.01')
        self.assertIn('EX-001', [e['code'] for e in result['errors']])

    def test_a_tax_candidate_keeps_tax_required(self):
        result = self.invoice(tax_candidates=[{'source': 'ai', 'value': '1.00'}])
        self.assertIn('EX-001', [e['code'] for e in result['errors']])

    def test_printed_tax_takes_part_in_the_sum(self):
        result = self.invoice(tax='1.00', total='21.00')
        self.assertEqual([e['code'] for e in result['errors']], [])
        self.assertTrue(any(c['rule'] == 'TOTAL-COMPONENTS' for c in result['checks']))

    def test_printed_tax_that_does_not_add_up_still_fails(self):
        self.assertIn('EX-005', [e['code'] for e in self.invoice(tax='1.00', total='22.00')['errors']])

    def receipt(self, **amounts):
        from validation.documents.financial_documents import evaluate_financial_document
        base = {'supplier': 'CROSS CHANNEL NETWORK SDN. BHD.', 'date': '2018-01-29', 'currency': 'MYR',
                'total_excluding_tax': '6.00', 'tax': '0.36', 'total_including_tax': '6.36', 'rounding': '-0.01', 'total': '6.35', **amounts}
        fields = [self.field(key, value) for key, value in base.items() if value is not None]
        items = [{'description': 'SCHNEIDER E15R 13A SWITCH', 'quantity': '1', 'unit_price': None, 'amount': '6.36', 'field_states': {}}]
        return evaluate_financial_document('receipt', {'raw_json': {}}, fields, items)

    def test_receipt_chain_through_tax_and_rounding(self):
        result = self.receipt()
        self.assertEqual(result['errors'], [])
        self.assertIn('6.00 + 0.36 tax - 0.01 rounding = 6.35', next(c for c in result['checks'] if c['rule'] == 'RECEIPT-TOTAL')['message'])

    def test_receipt_tax_inclusive_amount_must_agree(self):
        self.assertIn('RECEIPT-TOTAL', [e['code'] for e in self.receipt(total_including_tax='6.50')['errors']])

    def test_receipt_total_that_does_not_add_up_fails(self):
        self.assertIn('RECEIPT-TOTAL', [e['code'] for e in self.receipt(total='7.00')['errors']])

    def test_receipt_service_charge_with_no_tax_reconciles_from_printed_amounts(self):
        result = self.receipt(total_excluding_tax=None, subtotal='18.20', tax=None, total_including_tax=None, charges='1.82', rounding='-0.02', total='20.00')
        self.assertFalse([e for e in result['errors'] if e['code'] == 'RECEIPT-TOTAL'])
        self.assertIn('18.20 + 1.82 charges - 0.02 rounding = 20.00', next(c for c in result['checks'] if c['rule'] == 'RECEIPT-TOTAL')['message'])
