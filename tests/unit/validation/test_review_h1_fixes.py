"""Horizon 1 review fixes (24 Nov demo bugs d and e): printed tax vs printed total, checks that name a field, and the
stored file name using the bill's own printed number. No database."""
import unittest

from services.extraction.persistence.persistence import _usable_document_identifier


def field(key, value):
    return {'field_key': key, 'extraction_state': 'resolved' if value is not None else 'missing', 'normalized_value': value,
            'current_value': value, 'raw_value': value, 'resolution': 'parser_preserved', 'candidates': [], 'evidence': []}


class TaxLargerThanTotalTests(unittest.TestCase):
    """Bug d: a receipt read as tax 206.00 and total 20.60 must not pass as 'read from the document'."""

    def receipt(self, **amounts):
        from validation.documents.financial_documents import evaluate_financial_document
        base = {'supplier': 'RENGAS STATIONERY', 'date': '2026-11-03', 'currency': 'MYR', 'tax': '206.00', 'total': '20.60', **amounts}
        fields = [field(key, value) for key, value in base.items() if value is not None]
        return evaluate_financial_document('receipt', {'raw_json': {}}, fields, [])

    def test_swapped_tax_and_total_is_an_error_on_the_tax_field(self):
        result = self.receipt()
        error = next(e for e in result['errors'] if e['code'] == 'RECEIPT-TAX_PLAUSIBLE')
        self.assertEqual((error['field'], error['severity']), ('tax', 'high'))
        self.assertIn('206.00', error['message'])
        self.assertFalse(result['validation_passed'])

    def test_plausible_tax_raises_nothing(self):
        result = self.receipt(tax='1.17', total='20.60')
        self.assertFalse([e for e in result['errors'] if e['code'] == 'RECEIPT-TAX_PLAUSIBLE'])

    def test_tax_equal_to_total_is_not_flagged_by_this_rule(self):
        # Odd but not proof of a swap; other checks still apply.
        self.assertFalse([e for e in self.receipt(tax='20.60')['errors'] if e['code'] == 'RECEIPT-TAX_PLAUSIBLE'])

    def test_a_check_that_could_not_run_names_its_field(self):
        result = self.receipt(tax='1.17')
        total = next(c for c in result['checks'] if c['rule'] == 'RECEIPT-TOTAL')
        self.assertEqual(total['checkStatus'], 'NOT_CHECKED')
        self.assertEqual(total['field'], 'total')

    def test_invoice_with_tax_above_total_fails_ex005_on_tax(self):
        from validation.documents.document_validation import evaluate_invoice
        fields = [field('supplier', 'SELADAH CONSTRUCTION SDN BHD'), field('invoice', 'SCC-2611-0412'), field('date', '2026-11-02'),
                  field('currency', 'MYR'), field('tax', '206.00'), field('total', '20.60')]
        result = evaluate_invoice({'id': None, 'kind': 'invoice', 'category': 'accounts_payable'}, {'raw_json': {}}, fields, [])
        ex005 = [e for e in result['errors'] if e['code'] == 'EX-005']
        self.assertTrue(ex005)
        self.assertIn('larger than the printed total', ex005[0]['message'])


class StoredNameIdentifierTests(unittest.TestCase):
    """Bug e: a bill whose number was read into `bill_number` must not be named NONUMBER."""

    def test_bill_number_is_used_for_bills(self):
        self.assertEqual(_usable_document_identifier([field('bill_number', 'INV-88123')], 'bill'), 'INV-88123')

    def test_bill_number_wins_over_a_secondary_reference(self):
        fields = [field('reference', 'ACC-5521'), field('bill_number', 'KT-BL-2611-3358')]
        self.assertEqual(_usable_document_identifier(fields, 'bill'), 'KT-BL-2611-3358')

    def test_note_number_wins_over_the_related_invoice(self):
        fields = [field('invoice', 'PBS-INV-2610-100'), field('credit_note_number', 'PBS-CN-2611-017')]
        self.assertEqual(_usable_document_identifier(fields, 'credit_note'), 'PBS-CN-2611-017')

    def test_placeholder_values_are_still_skipped(self):
        self.assertIsNone(_usable_document_identifier([field('bill_number', 'N/A')], 'bill'))


if __name__ == '__main__':
    unittest.main()
