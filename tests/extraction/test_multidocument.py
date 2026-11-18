"""Offline profile/contract tests. Live Docling is tested separately."""
from copy import deepcopy
import unittest

from services.extraction.classification.classify_document import classify_document
from services.extraction.parsing.map_invoice import map_document
from services.extraction.parsing.extraction_v1 import adapt_result
from services.extraction.contracts.accounting_contract import map_m2_result
from validation.documents.document_validation import evaluate_document


def source(title, *labels):
    return {'schema_name': 'DoclingDocument', 'pages': {'1': {'size': {'width': 595, 'height': 842}}},
            'texts': [{'text': text, 'label': 'text', 'self_ref': f'#/texts/{i}',
                       'prov': [{'page_no': 1, 'bbox': {'l': 10, 'r': 500, 't': 10, 'b': 30, 'coord_origin': 'TOPLEFT'}}]}
                      for i, text in enumerate((title, *labels))]}


def contract(doc):
    mapped = map_document(doc)
    mapped['run_id'] = 'synthetic-offline'
    v1 = adapt_result(mapped, 'SYN-PROFILE', 'mapped.json', 'a' * 64).model_dump()
    raw = {'schema_version': '0.1.0-proposed', 'mode': 'module2_public_result',
           'document_id': 'SYN-PROFILE', 'document_type': classify_document(doc),
           'status': 'human_review_required', 'review_required': True, 'accounting_approval_performed': False,
           'safety': {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False},
           'provenance': {}, 'fields': v1['fields'], 'line_items': v1['line_items'], 'issues': v1['issues'],
           'pretax_structure': v1['provenance'].get('pretax_structure')}
    return raw, map_m2_result(raw, 'SYN-PROFILE')


class MultiDocumentTests(unittest.TestCase):
    def test_credit_note_with_empty_shared_aliases_can_be_stored(self):
        # Every credit note used to fail its job here: three empty cells share one stored key.
        raw, mapped = contract(source('CREDIT NOTE', 'Supplier: Synthetic', 'Credit note number: CN-1',
                                     'Date: 2026-01-31', 'Currency: MYR', 'Total: 10.00'))
        fields = {f['field_key']: f for f in mapped['fields']}
        self.assertEqual(fields['reference']['current_value'], 'CN-1')
        self.assertEqual(fields['total']['current_value'], '10.00')

    def test_two_real_values_on_one_stored_key_still_fail(self):
        raw, _ = contract(source('CREDIT NOTE', 'Credit note number: CN-1', 'Date: 2026-01-31', 'Total: 10.00'))
        cell = dict(raw['fields']['credit_note_number'])
        raw['fields']['reference_number'] = {**cell, 'value': 'OTHER', 'raw_text': 'OTHER'}
        with self.assertRaisesRegex(ValueError, 'Conflicting aliases'):
            map_m2_result(raw, 'SYN-PROFILE')

    def test_bill_profile_maps_without_invoice_requirements(self):
        raw, mapped = contract(source('Utility Bill', 'Issuer: Synthetic Utility', 'Amount due: 0.00',
                                     'Account number: SYN-A', 'Bill reference: SYN-B', 'Due date: 2026-10-31'))
        fields = {f['field_key']: f for f in mapped['fields']}
        self.assertEqual(fields['amount_due']['current_value'], '0.00')
        self.assertIsNone(fields['total']['current_value'])
        self.assertEqual(fields['supplier']['current_value'], 'Synthetic Utility')
        self.assertIsNone(fields['currency']['current_value'])
        self.assertNotIn('invoice', fields)
        check = evaluate_document({'kind': 'bill'}, {'raw_json': raw}, mapped['fields'], [])
        # Revised for validation-v2.1: the profile rule no longer repeats the same
        # field finding as a second exception. It stays named, at its stricter severity.
        self.assertEqual({e['code'] for e in check['errors']}, {'BILL-REQUIRED'})
        merged = {e['field']: e for e in check['errors'] if e.get('relatedRules') == ['PROFILE-REQUIRED']}
        self.assertEqual(set(merged), {'date', 'currency'})
        self.assertTrue(all(e['severity'] == 'high' for e in merged.values()))
        self.assertFalse(check['coverage_complete'])
        self.assertTrue(any(c['rule'] == 'BILL-AMOUNT_DUE' and c['checkStatus'] == 'NOT_CHECKED' for c in check['checks']))

    def test_receipt_payment_method_is_evidenced_text_not_paid_status(self):
        raw, mapped = contract(source('Receipt', 'Merchant: Synthetic Retail', 'Total: 10.60',
                                     'Transaction time: 14:35:20', 'Payment method: Card'))
        fields = {f['field_key']: f for f in mapped['fields']}
        self.assertEqual(fields['payment_method']['current_value'], 'Card')
        self.assertTrue(fields['payment_method']['evidence'][0]['source_ref'])
        self.assertIsNone(fields.get('reference', {}).get('current_value'))
        self.assertIsNone(fields.get('receipt_number', {}).get('current_value'))
        self.assertNotIn('paid', fields)
        self.assertIsNone(mapped['category'])
        self.assertFalse(evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, mapped['fields'], [])['coverage_complete'])

    def test_unknown_retains_provisional_source_reads_without_trusting_filename(self):
        doc = source('Meeting agenda', 'Supplier: Fiction', 'Total amount: 999.00')
        doc['origin'] = {'filename': 'invoice.pdf'}
        raw, mapped = contract(doc)
        fields = {f['field_key']: f for f in mapped['fields']}
        self.assertEqual(fields['supplier']['candidates'][0]['value'], 'Fiction')
        self.assertEqual(fields['total']['candidates'][0]['value'], '999.00')
        self.assertTrue(all(f['current_value'] is None and f['normalized_value'] is None for f in mapped['fields']))
        self.assertTrue(fields['total']['evidence'][0]['source_ref'])
        self.assertEqual(fields['total']['evidence'][0]['candidate_basis'], 'provisional_document_type')
        self.assertEqual(mapped['line_items'], [])
        self.assertEqual(raw['document_type']['value'], 'unknown')
        check = evaluate_document({'kind': 'invoice'}, {'raw_json': raw}, mapped['fields'], [])
        self.assertTrue(any(e['code'] == 'EX-002' and e['field'] == 'document_type' for e in check['errors']))
        from services.extraction.contracts.accounting_contract import approved_snapshot
        with self.assertRaisesRegex(ValueError, 'classification requires review'):
            approved_snapshot({'kind': 'invoice', 'category': 'accounts_payable'}, {'raw_json': raw}, mapped['fields'], [])

    def test_provisional_schema_rejects_selected_values(self):
        from services.extraction.parsing.extraction_v1 import ExtractionResultV1
        doc = source('Document', 'Supplier: Fiction', 'Total: 999.00')
        before = deepcopy(doc)
        mapped = map_document(doc)
        mapped['run_id'] = 'synthetic-provisional'
        parsed = adapt_result(mapped, 'SYN-PROFILE', 'mapped.json', 'a' * 64)
        self.assertEqual(doc, before)
        self.assertEqual(parsed.field_schema, 'unknown_provisional_fields_v1')
        data = parsed.model_dump()
        data['fields']['total_amount'].update(state='resolved', value='999.00')
        with self.assertRaisesRegex(ValueError, 'unselected candidates'):
            ExtractionResultV1.model_validate(data)

    def test_overlap_remains_ambiguous_with_both_sources(self):
        for doc in (source('Invoice', 'Utility Bill'), source('Bill / Invoice')):
            raw, mapped = contract(doc)
            self.assertEqual(raw['document_type']['status'], 'ambiguous')
            self.assertEqual(len(raw['document_type']['candidates']), 2)
            self.assertEqual(mapped['fields'], [])

    def test_layout_b_optional_invoice_fields_share_existing_boundary(self):
        raw, mapped = contract(source('Tax Invoice', 'Vendor: Synthetic', 'Invoice no: SYN-B',
            'Invoice date: 2026-10-19', 'Customer: Synthetic buyer', 'Discount: 0.00'))
        fields = {f['field_key']: f for f in mapped['fields']}
        self.assertEqual(fields['customer']['current_value'], 'Synthetic buyer')
        self.assertEqual(fields['discount']['normalized_value'], '0.00')
        self.assertEqual(fields['invoice']['current_value'], 'SYN-B')
        self.assertNotIn('due_date', raw['fields'])

    def test_malformed_values_remain_unresolved_with_original_candidates(self):
        for label, key in [('Amount due: NaN', 'amount_due'), ('Bill date: 2026-02-30', 'bill_date'),
                           ('Currency: RM', 'currency'), ('Amount due: 1.001', 'amount_due')]:
            raw, _ = contract(source('Bill', label))
            field = raw['fields'][key]
            self.assertIsNone(field['value'])
            self.assertEqual(field['state'], 'ambiguous')
            self.assertEqual(field['candidates'][0]['raw_text'], label)

    def test_conflicting_labels_do_not_select_a_value(self):
        doc = source('Receipt', 'Total: 10.00', 'Total amount: 99.00')
        before = deepcopy(doc)
        raw, _ = contract(doc)
        self.assertEqual(doc, before)
        self.assertEqual(raw['fields']['total_amount']['state'], 'ambiguous')
        self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_docling_merged_label_block_keeps_spans_and_time_colons(self):
        block = 'Merchant: Synthetic Retail Transaction date: 2026-10-19 Transaction time: 14:35:20 Total: 10.60 Payment method: Card'
        raw, mapped = contract(source('Retail Receipt', block))
        self.assertEqual(raw['fields']['merchant']['value'], 'Synthetic Retail')
        self.assertEqual(raw['fields']['transaction_time']['value'], '14:35:20')
        field = raw['fields']['total_amount']
        self.assertEqual(field['value'], '10.60')
        ev = field['evidence'][0]
        self.assertEqual(ev['text'], block)
        self.assertEqual(ev['text'][slice(*ev['text_span'])].strip(), field['raw_text'])
        self.assertEqual(ev['bbox_scope'], 'source_block')

    def test_label_words_in_prose_do_not_start_extraction(self):
        raw, _ = contract(source('Invoice', 'Please check Supplier: Fiction Total: 999.00'))
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertIsNone(raw['fields']['total_amount']['value'])

    def charges_document(self, amount='80.00'):
        doc = source('Utility Bill', 'Amount due: 80.00')
        cells = []
        for row, values in enumerate([['Description', 'Line total'], ['Service charge', amount]]):
            for col, text in enumerate(values):
                cells.append({'text': text, 'start_row_offset_idx': row, 'start_col_offset_idx': col,
                              'row_span': 1, 'col_span': 1, 'column_header': row == 0 and col == 1,
                              'bbox': {'l': 10, 'r': 500, 't': 10, 'b': 30, 'coord_origin': 'TOPLEFT'}})
        doc['tables'] = [{'self_ref': '#/tables/0', 'prov': [{'page_no': 1}],
                          'data': {'num_rows': 2, 'num_cols': 2, 'table_cells': cells}}]
        return doc

    def test_two_column_charge_preserves_missing_quantity_and_unit_price(self):
        raw, mapped = contract(self.charges_document())
        row = mapped['line_items'][0]
        self.assertEqual(row['amount'], '80.00')
        self.assertIsNone(row['quantity'])
        self.assertIsNone(row['unit_price'])
        self.assertEqual(row['field_states']['quantity'], 'missing')
        self.assertEqual(row['evidence']['amount'][0]['source_ref'], '#/tables/0/data/table_cells/3')

    def test_malformed_charge_is_not_coerced_or_defaulted(self):
        raw, mapped = contract(self.charges_document('80.001'))
        row = mapped['line_items'][0]
        self.assertIsNone(row['amount'])
        self.assertEqual(row['field_states']['amount'], 'ambiguous')
        self.assertEqual(raw['line_items'][0]['line_total']['raw_text'], '80.001')

    def test_unsupported_column_labels_are_not_guessed(self):
        doc = self.charges_document()
        doc['tables'][0]['data']['table_cells'][0]['text'] = 'Mystery column'
        raw, mapped = contract(doc)
        self.assertEqual(mapped['line_items'], [])
        self.assertTrue(any(issue['code'] == 'UNSUPPORTED_TABLE' for issue in raw['issues']))


if __name__ == '__main__':
    unittest.main()
