"""Independent offline M3 rule contracts; no DB, network, parsing or approval."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from validation.engine import EXCEPTION_SEVERITY, validate_invoice_document
from validation.duplicate import find_duplicate_invoices
from validation.document_validation import evaluate_document
from services.extraction.accounting_contract import map_m2_result


def invoice(**updates):
    return {'invoice_number': 'M3-SYN-001', 'vendor_name': 'Synthetic M3 Supplies Ltd',
            'invoice_date': '2026-10-19', 'due_date': '2026-11-19', 'currency': 'MYR',
            'subtotal': '50.00', 'tax_amount': '0.00', 'total_amount': '50.00',
            'line_items': [{'description': 'Synthetic item', 'quantity': '2', 'unit_price': '25.00', 'amount': '50.00'}], **updates}


class M3ValidationTests(unittest.TestCase):
    def engine(self, payload, **context):
        return validate_invoice_document(payload, **{'known_vendors': [{'id': 'SYN-V', 'vendor_name': 'Synthetic M3 Supplies Ltd'}],
                                                     'existing_invoices': [], **context})

    def test_each_core_exception_is_independently_exercised(self):
        cases = [
            ('EX-001', invoice(invoice_number=None)),
            ('EX-002', invoice(currency=123)),
            ('EX-003', invoice(line_items=[{'description':'Synthetic', 'quantity':'2', 'unit_price':'25.00', 'amount':'49.00'}])),
            ('EX-004', invoice(subtotal='40.00', total_amount='40.00')),
            ('EX-005', invoice(total_amount='99.00')),
            ('EX-006', invoice(due_date='2026-10-01')),
        ]
        for code, payload in cases:
            with self.subTest(code=code):
                before = deepcopy(payload)
                result = self.engine(payload)
                self.assertEqual({e['code'] for e in result['errors']}, {code})
                self.assertEqual(result['errors'][0]['severity'], EXCEPTION_SEVERITY[code])
                self.assertTrue(result['errors'][0]['message'])
                self.assertEqual(payload, before)

    def test_valid_zero_and_decimal_inputs_have_no_false_positives(self):
        for payload in (invoice(), invoice(subtotal='0.00', total_amount='0.00',
                line_items=[{'description':'Synthetic', 'quantity':'2', 'unit_price':'0.00', 'amount':'0.00'}])):
            self.assertEqual(self.engine(payload)['errors'], [])

    def test_nulls_are_not_the_string_none(self):
        self.assertEqual(find_duplicate_invoices([{'vendor_name': None, 'invoice_number': None}] * 2), [])
        self.assertEqual(find_duplicate_invoices([{'vendor_name': 'Synthetic', 'invoice_number': None}] * 2), [])

    def test_incomplete_duplicate_key_is_not_claimed_checked(self):
        result = self.engine(invoice(invoice_number=None))
        self.assertTrue(any(c['rule']=='EX-007' and c['checkStatus']=='NOT_CHECKED' for c in result['checks']))

    def test_unrelated_historical_duplicate_pair_does_not_flag_target(self):
        existing = [invoice(invoice_number='OLD', source_file='old-a'), invoice(invoice_number='OLD', source_file='old-b')]
        self.assertEqual(self.engine(invoice(), existing_invoices=existing)['errors'], [])

    def test_actual_duplicate_has_high_severity_and_real_reference(self):
        result = self.engine(invoice(), existing_invoices=[invoice(source_file='SYN-DOC-OTHER')])
        error = next(e for e in result['errors'] if e['code'] == 'EX-007')
        self.assertEqual(error['duplicate_of'], 'SYN-DOC-OTHER')
        self.assertEqual(error['severity'], 'high')

    def test_current_document_identity_is_excluded(self):
        self.assertEqual(self.engine(invoice(document_id='SELF'), existing_invoices=[invoice(document_id='SELF')])['errors'], [])

    def test_vendor_fuzzy_and_unknown_require_review_without_extract_confidence(self):
        for vendor in ('Synthetic M3 Supplies', 'Completely Unrelated Synthetic Entity'):
            result = self.engine(invoice(vendor_name=vendor))
            error = next(e for e in result['errors'] if e['code'] == 'EX-008')
            self.assertEqual(error['severity'], 'medium')
            self.assertIn('similarity', error['vendor_candidate'])
            self.assertNotIn('confidence', error['vendor_candidate'])

    def test_no_catalogue_is_not_checked_not_an_invented_vendor_error(self):
        result = self.engine(invoice(), known_vendors=[])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['vendor_match']['status'], 'NOT_CHECKED')
        self.assertTrue(any(c['rule']=='EX-008' and c['checkStatus']=='NOT_CHECKED' for c in result['checks']))

    def test_standalone_ex009_is_preserved_without_live_po_claim(self):
        po = {'po_number':'SYN-PO', 'vendor_name':'Synthetic M3 Supplies Ltd',
              'line_items':[{'description':'Synthetic item','quantity':'2','unit_price':'20.00'}], 'total_amount':'40.00'}
        result = self.engine(invoice(), purchase_order=po)
        self.assertEqual({e['code'] for e in result['errors']}, {'EX-009'})
        self.assertEqual(result['errors'][0]['severity'], 'high')
        self.assertEqual(result['po_match']['status'], 'REVIEW_REQUIRED')

    def test_malformed_date_type_and_line_type_are_ex002(self):
        for payload in (invoice(invoice_date=123), invoice(invoice_date='20261019'),
                        invoice(line_items=[{'description':'Synthetic','quantity':'oops','unit_price':'25.00','amount':'50.00'}])):
            self.assertEqual({e['code'] for e in self.engine(payload)['errors']}, {'EX-002'})

    def test_corrected_effective_value_wins_over_invalid_raw_parser_state(self):
        raw = json.loads((Path(__file__).parent/'extraction/data/contracts/syn001_m2.json').read_text())
        raw['fields']['total_amount'].update(value=None, state='ambiguous', resolution='unresolved')
        mapped = map_m2_result(raw, raw['document_id'])
        doc = {'kind':'invoice','category':'accounts_payable'}
        result = evaluate_document(doc, {'raw_json':raw}, mapped['fields'], mapped['line_items'])
        self.assertTrue(any(e['code']=='EX-002' for e in result['errors']))
        total = next(f for f in mapped['fields'] if f['field_key']=='total')
        total.update(current_value='50.00', corrected_by='synthetic-reviewer', corrected_at='2026-10-19')
        self.assertEqual(evaluate_document(doc, {'raw_json':raw}, mapped['fields'], mapped['line_items'])['errors'], [])


if __name__ == '__main__':
    unittest.main()
