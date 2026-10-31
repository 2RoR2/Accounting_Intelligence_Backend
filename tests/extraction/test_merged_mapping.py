"""Explicit-label regressions; synthetic text and replayed Docling artifacts only."""
from copy import deepcopy
import unittest
from tests.extraction.test_multidocument import source, contract
from services.extraction.parsing.map_invoice import map_document

PRESERVED_HEADER = (
    'INVOICE P4B SYNTHETIC TEST DOCUMENT - ab4f473e08fb '
    'Example Stationery Synthetic Invoice Number: '
    'P4B-REAL-DOCLING-SMOKE-ab4f473e08fb Invoice Date: '
    '2026-10-01 Currency: MYR Bill To: Synthetic Acceptance Tenant')


class MergedMappingTests(unittest.TestCase):
    def test_preserved_heading_recovers_only_labelled_values(self):
        doc = source(PRESERVED_HEADER,
            'Description Quantity Unit Price Amount Synthetic stationery 2 25.00 50.00',
            'Subtotal: 50.00 Tax: 0.00 Total: MYR 50.00')
        before = deepcopy(doc)
        raw, canonical = contract(doc)
        expected = {'invoice_number': 'P4B-REAL-DOCLING-SMOKE-ab4f473e08fb',
                    'invoice_date': '2026-10-01', 'currency': 'MYR',
                    'subtotal': '50.00', 'tax_amount': '0.00', 'total_amount': '50.00'}
        self.assertEqual(raw['document_type']['value'], 'invoice')
        for key, value in expected.items():
            self.assertEqual(raw['fields'][key]['value'], value)
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertNotIn('buyer_name', raw['fields'])  # Bill To is a delimiter, not a guessed buyer.
        self.assertEqual(canonical['line_items'], [])
        self.assertEqual(doc, before)
        field = raw['fields']['currency']
        ev = field['evidence'][0]
        self.assertEqual(ev['text'], PRESERVED_HEADER)
        self.assertEqual(ev['text'][slice(*ev['text_span'])].strip(), 'Currency: MYR')
        self.assertEqual(ev['source_ref'], '#/texts/0')
        self.assertEqual(ev['page'], 1)
        self.assertIsNotNone(ev['bbox'])
        self.assertEqual(ev['bbox_original'], doc['texts'][0]['prov'][0]['bbox'])

    def test_heading_with_explicit_supplier_and_multiple_fields(self):
        raw, _ = contract(source('INVOICE Supplier: Synthetic Vendor Invoice Number: SYN-9 '
            'Invoice Date: 2026-10-01 Currency: MYR Total: MYR 25.00'))
        self.assertEqual(raw['fields']['supplier_name']['value'], 'Synthetic Vendor')
        self.assertEqual(raw['fields']['invoice_number']['value'], 'SYN-9')
        self.assertEqual(raw['fields']['total_amount']['value'], '25.00')

    def test_invoice_id_uses_existing_canonical_alias(self):
        raw, canonical = contract(source('Invoice', 'Vendor: Synthetic Invoice ID: SYN-ID Currency: MYR'))
        self.assertEqual(raw['fields']['invoice_number']['value'], 'SYN-ID')
        self.assertEqual(next(f for f in canonical['fields'] if f['field_key'] == 'invoice')['current_value'], 'SYN-ID')

    def test_conflicting_duplicate_values_are_not_selected(self):
        raw, _ = contract(source('INVOICE Invoice Number: SYN-1 Invoice Date: 2026-10-01 '
            'Currency: MYR Total: 50.00 Total: 99.00'))
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertEqual({c['value'] for c in raw['fields']['total_amount']['candidates']}, {'50.00', '99.00'})

    def test_missing_labels_remain_missing(self):
        raw, _ = contract(source(PRESERVED_HEADER))
        for key in ['supplier_name', 'subtotal', 'tax_amount', 'total_amount']:
            self.assertIsNone(raw['fields'][key]['value'])

    def test_malformed_typed_values_remain_ambiguous(self):
        for label, key in [('Currency: RM', 'currency'), ('Invoice Date: 2026-02-30', 'invoice_date'),
                           ('Total: NaN', 'total_amount'), ('Total: 1.001', 'total_amount')]:
            raw, _ = contract(source('INVOICE Invoice Number: SYN-1 Date: 2026-10-01 ' + label))
            self.assertIsNone(raw['fields'][key]['value'])
            self.assertIn(raw['fields'][key]['state'], {'ambiguous', 'conflict'})

    def test_label_words_inside_supplier_name_are_not_labels(self):
        raw, _ = contract(source('INVOICE Supplier: Total Invoice Date Supplies Sdn Bhd '
            'Invoice Number: SYN-1 Invoice Date: 2026-10-01 Currency: MYR'))
        self.assertEqual(raw['fields']['supplier_name']['value'], 'Total Invoice Date Supplies Sdn Bhd')
        self.assertEqual(raw['fields']['invoice_date']['value'], '2026-10-01')

    def test_colon_label_inside_free_text_is_ambiguous_not_a_truncated_supplier(self):
        raw, _ = contract(source('Invoice', 'Supplier: Synthetic Total: Care Services Invoice Number: SYN-1'))
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_currency_prefix_never_resolves_conflicting_currency(self):
        for currency in ['Currency: USD ', 'Currency: MYR Currency: USD ']:
            raw, _ = contract(source('INVOICE Invoice Number: SYN-1 Invoice Date: 2026-10-01 '
                + currency + 'Total: MYR 50.00'))
            self.assertIsNone(raw['fields']['total_amount']['value'])
            self.assertEqual(raw['fields']['total_amount']['state'], 'ambiguous')

    def test_explicit_currency_token_has_source_evidence(self):
        raw, _ = contract(source('INVOICE Invoice Number: SYN-1 Invoice Date: 2026-10-01 Total: MYR 50.00'))
        self.assertEqual(raw['fields']['currency']['value'], 'MYR')
        self.assertEqual(raw['fields']['total_amount']['value'], '50.00')
        self.assertIn('MYR', raw['fields']['currency']['evidence'][0]['text'])

    def test_label_at_start_of_supplier_value_is_not_a_verified_identifier(self):
        raw, _ = contract(source('Invoice', 'Supplier: Invoice ID: Supplies Sdn Bhd'))
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertIsNone(raw['fields']['invoice_number']['value'])
        self.assertEqual(raw['fields']['invoice_number']['state'], 'ambiguous')

    def test_prose_and_descriptions_do_not_enable_label_scanning(self):
        for block in ['Please check Supplier: Fiction Total: 999.00',
                      'Description: Invoice Number: Fiction Total: 999.00']:
            raw, _ = contract(source('Invoice', block))
            self.assertIsNone(raw['fields']['invoice_number']['value'])
            self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_unsupported_and_ambiguous_documents_remain_empty(self):
        # Current features additionally supports credit/debit notes.
        for title in ['Quotation', 'Pro forma invoice', 'Purchase Order', 'Invoice / Receipt']:
            raw, canonical = contract(source(title, 'Supplier: Fiction Invoice Number: SYN-1 Total: 999.00'))
            self.assertEqual(raw['document_type']['value'], 'unknown')
            self.assertEqual(canonical['fields'], [])
            self.assertEqual(canonical['line_items'], [])

    def test_existing_bill_and_receipt_profiles_keep_exact_values(self):
        for title, block, key in [
            ('UTILITY BILL Account number: SYN-A Amount due: 80.00 Currency: MYR', '', 'amount_due'),
            ('RETAIL RECEIPT Receipt number: SYN-R Total: 80.00 Currency: MYR', '', 'total_amount')]:
            raw, _ = contract(source(title))
            self.assertEqual(raw['fields'][key]['value'], '80.00')
            self.assertEqual(raw['fields']['currency']['value'], 'MYR')
