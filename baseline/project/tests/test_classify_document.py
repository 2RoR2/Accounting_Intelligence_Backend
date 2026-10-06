"""Synthetic rule tests, not held-out classification accuracy."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from classify_document import classify_document

def document(*texts):
    return {'schema_name': 'DoclingDocument', 'texts': [
        {'text': t, 'label': 'text', 'self_ref': f'#/texts/{i}',
         'prov': [{'page_no': 1, 'bbox': None}]} for i, t in enumerate(texts)]}

class ClassifierTests(unittest.TestCase):
    def test_supported_titles(self):
        for title, expected in [('INVOICE - SYNTHETIC TEST DATA', 'invoice'),
                                ('Tax Invoice', 'invoice'), ('Utility Bill', 'bill'),
                                ('Sales Receipt', 'receipt')]:
            with self.subTest(title=title):
                self.assertEqual(classify_document(document(title))['value'], expected)

    def test_bill_to_and_invoice_references_do_not_classify(self):
        r = classify_document(document('Bill to: Example Ltd', 'Invoice number: SYN-001',
                                       'Keep this receipt for your records'))
        self.assertEqual((r['value'], r['status']), ('unknown', 'unknown'))

    def test_incompatible_headings_are_ambiguous(self):
        r = classify_document(document('INVOICE', 'RECEIPT'))
        self.assertEqual((r['value'], r['status']), ('unknown', 'ambiguous'))
        self.assertEqual(len(r['candidates']), 2)

    def test_repeated_same_type_is_not_ambiguous(self):
        self.assertEqual(classify_document(document('INVOICE', 'Tax Invoice'))['value'], 'invoice')

    def test_unsupported_heading_blocks_supported_guess(self):
        r = classify_document(document('Credit Note', 'Invoice'))
        self.assertEqual((r['value'], r['status']), ('unknown', 'unsupported'))
        self.assertEqual(len(r['unsupported_headings']), 1)

    def test_proforma_is_not_invoice(self):
        self.assertEqual(classify_document(document('Pro forma invoice'))['value'], 'unknown')

    def test_empty_document_is_unknown(self):
        self.assertEqual(classify_document(document())['status'], 'unknown')

    def test_furniture_ignored(self):
        d = document('RECEIPT')
        d['texts'][0]['content_layer'] = 'furniture'
        self.assertEqual(classify_document(d)['value'], 'unknown')

    def test_table_cell_ignored(self):
        d = document('RECEIPT')
        d['texts'][0]['label'] = 'table_cell'
        self.assertEqual(classify_document(d)['value'], 'unknown')

    def test_evidence_preserved_without_mutation(self):
        d = document('Invoice')
        before = deepcopy(d)
        r = classify_document(d)
        self.assertEqual(d, before)
        self.assertEqual(r['candidates'][0]['evidence']['prov'], d['texts'][0]['prov'])
        self.assertEqual(r['candidates'][0]['evidence']['source_ref'], '#/texts/0')
        self.assertTrue(r['review_required'])

    def test_wrong_input_schema_rejected(self):
        with self.assertRaises(ValueError): classify_document({'schema_name': 'Other'})

if __name__ == '__main__':
    unittest.main()


